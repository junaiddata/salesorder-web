from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, Count, Q
from django.core.paginator import Paginator
from django.utils import timezone
from django.http import Http404
from datetime import date, datetime
from decimal import Decimal

from .models import AlabamaSalesLine, AlabamaPurchaseLine, AlabamaSAPQuotation, AlabamaSAPQuotationItem, AlabamaSalesmanMapping

# Cache for salesman mappings; cleared when mappings are added/edited/deleted
_salesman_mapping_cache = None


def _get_salesman_mapping_dict():
    """Load salesman mappings from DB (cached until mappings change)."""
    global _salesman_mapping_cache
    if _salesman_mapping_cache is not None:
        return _salesman_mapping_cache
    try:
        rows = AlabamaSalesmanMapping.objects.all().values_list('raw_name', 'normalized_name')
        _salesman_mapping_cache = {r[0].lower(): r[1] for r in rows}
        return _salesman_mapping_cache
    except Exception:
        return {}


def _clear_salesman_mapping_cache():
    """Clear cache when mappings are modified."""
    global _salesman_mapping_cache
    _salesman_mapping_cache = None


def normalize_alabama_salesman(name):
    """Merge salesman name variants to canonical names using Settings mappings (e.g. A.KADER -> KADER)."""
    if not name or not str(name).strip():
        return name
    key = str(name).strip().lower()
    mapping = _get_salesman_mapping_dict()
    return mapping.get(key, name.strip())


def _expand_alabama_salesman_names_for_scope(canonical_names):
    """
    Expand names from SALES_USER_MAP with all raw/normalized variants from AlabamaSalesmanMapping
    (Settings). So e.g. canonical 'KADER' also matches Excel 'A.KADER', 'A. KADER', etc.
    """
    if not canonical_names:
        return set()
    result = set()
    for n in canonical_names:
        if n and str(n).strip():
            result.add(str(n).strip())
    canon_lower = {x.lower() for x in result}
    try:
        for raw, norm in AlabamaSalesmanMapping.objects.values_list('raw_name', 'normalized_name'):
            nl = (norm or '').strip().lower()
            if nl and nl in canon_lower:
                r = (raw or '').strip()
                v = (norm or '').strip()
                if r:
                    result.add(r)
                if v:
                    result.add(v)
    except Exception:
        pass
    return result


def alabama_salesman_scope_q(user, field='sales_employee'):
    """
    Return Q filter for Alabama models by salesman (sales_employee or salesman_name).
    Reuses SALES_USER_MAP from so.views, expanded with AlabamaSalesmanMapping variants.
    Admin/superuser/manager sees all.
    """
    try:
        if user.is_superuser or (user.username or '').strip().lower() == 'manager':
            return Q()
        if hasattr(user, 'role') and user.role.role == 'Admin':
            return Q()
    except (AttributeError, TypeError):
        pass
    try:
        from so.models import Role
        role = Role.objects.get(user=user)
        if role.role == 'Admin':
            return Q()
    except (Role.DoesNotExist, AttributeError):
        pass

    from so.views import SALES_USER_MAP
    uname = (user.username or '').strip().lower()
    names = SALES_USER_MAP.get(uname)
    if names:
        expanded = _expand_alabama_salesman_names_for_scope(names)
        q = Q()
        for n in expanded:
            q |= Q(**{f'{field}__iexact': n})
        return q

    token = uname.replace('.', ' ').strip()
    if token:
        return Q(**{f'{field}__icontains': token})
    return Q(pk__in=[])


@login_required
def home(request):
    """Alabama homepage with stats, calendar widget, and sidebar."""
    from datetime import timedelta

    today = date.today()
    current_year = today.year
    current_month = today.month

    qs = AlabamaSalesLine.objects.all()

    def _gp_pct(gp, sales):
        s = float(sales or 0)
        return round((float(gp or 0) / s * 100), 1) if s else 0

    # Today
    today_qs = qs.filter(posting_date=today)
    today_sales = today_qs.aggregate(s=Sum('net_sales'))['s'] or 0
    today_gp = today_qs.aggregate(s=Sum('gross_profit'))['s'] or 0
    today_docs = today_qs.values('document_type', 'document_number').distinct().count()

    # This Week (Monday to today)
    today_weekday = today.weekday()  # Monday=0, Sunday=6
    week_start = today if today_weekday == 0 else today - timedelta(days=today_weekday)
    week_qs = qs.filter(posting_date__gte=week_start, posting_date__lte=today)
    week_sales = week_qs.aggregate(s=Sum('net_sales'))['s'] or 0
    week_gp = week_qs.aggregate(s=Sum('gross_profit'))['s'] or 0

    # Month
    month_qs = qs.filter(posting_date__year=current_year, posting_date__month=current_month)
    month_sales = month_qs.aggregate(s=Sum('net_sales'))['s'] or 0
    month_gp = month_qs.aggregate(s=Sum('gross_profit'))['s'] or 0

    # Year
    year_qs = qs.filter(posting_date__year=current_year)
    year_sales = year_qs.aggregate(s=Sum('net_sales'))['s'] or 0
    year_gp = year_qs.aggregate(s=Sum('gross_profit'))['s'] or 0

    # Calendar: days 1 to today with sales per day
    month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    month_days = []
    for day_num in range(1, today.day + 1):
        day_date = date(current_year, current_month, day_num)
        day_qs = qs.filter(posting_date=day_date)
        day_sales = day_qs.aggregate(s=Sum('net_sales'))['s'] or Decimal('0')
        day_gp = day_qs.aggregate(s=Sum('gross_profit'))['s'] or Decimal('0')
        month_days.append({
            'day': day_num,
            'date': day_date,
            'formatted_date': f"{month_names[current_month - 1]} {day_num}",
            'sales': day_sales,
            'gp': day_gp,
            'gp_pct': _gp_pct(day_gp, day_sales),
            'has_sales': bool(day_sales and day_sales > 0),
        })

    is_admin = request.user.is_superuser or request.user.is_staff or (
        request.user.username or ''
    ).strip().lower() == 'manager' or (
        hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Admin'
    )

    context = {
        'today_sales': today_sales,
        'today_gp': today_gp,
        'today_gp_pct': _gp_pct(today_gp, today_sales),
        'today_docs': today_docs,
        'week_sales': week_sales,
        'week_gp': week_gp,
        'week_gp_pct': _gp_pct(week_gp, week_sales),
        'month_sales': month_sales,
        'month_gp': month_gp,
        'month_gp_pct': _gp_pct(month_gp, month_sales),
        'year_sales': year_sales,
        'year_gp': year_gp,
        'year_gp_pct': _gp_pct(year_gp, year_sales),
        'month_days': month_days,
        'is_admin': is_admin,
        'today': today,
        'active_page': 'dashboard',
    }
    return render(request, 'alabama/home.html', context)


@login_required
def alabama_sales_home(request):
    """Alabama salesman dashboard - stats filtered by salesman scope."""
    today = date.today()
    current_year = today.year
    current_month = today.month

    scope_q = alabama_salesman_scope_q(request.user, field='sales_employee')
    qs = AlabamaSalesLine.objects.filter(scope_q)

    today_qs = qs.filter(posting_date=today)
    today_sales = today_qs.aggregate(s=Sum('net_sales'))['s'] or 0
    today_gp = today_qs.aggregate(s=Sum('gross_profit'))['s'] or 0

    month_qs = qs.filter(posting_date__year=current_year, posting_date__month=current_month)
    month_sales = month_qs.aggregate(s=Sum('net_sales'))['s'] or 0
    month_gp = month_qs.aggregate(s=Sum('gross_profit'))['s'] or 0

    year_qs = qs.filter(posting_date__year=current_year)
    year_sales = year_qs.aggregate(s=Sum('net_sales'))['s'] or 0
    year_gp = year_qs.aggregate(s=Sum('gross_profit'))['s'] or 0

    is_admin = request.user.is_superuser or request.user.is_staff or (
        request.user.username or ''
    ).strip().lower() == 'manager' or (
        hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Admin'
    )

    context = {
        'today_sales': today_sales,
        'today_gp': today_gp,
        'month_sales': month_sales,
        'month_gp': month_gp,
        'year_sales': year_sales,
        'year_gp': year_gp,
        'is_admin': is_admin,
        'active_page': 'dashboard',
    }
    return render(request, 'alabama/sales_home.html', context)


@login_required
def sales_summary_list(request):
    """Combined Sales Summary list (like combined_sales_invoices_list)."""
    from django.db.models import Sum, Value, DecimalField
    from django.db.models.functions import Coalesce

    today = date.today()
    current_year = today.year
    current_month = today.month

    # Filters
    q = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    document_type_filter = request.GET.get('document_type', '').strip()
    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()
    total_range = request.GET.get('total', '').strip()
    page_size = int(request.GET.get('page_size', 25)) or 25
    page_size = max(5, min(100, page_size))

    # Build aggregate: group by (document_type, document_number)
    # We need document-level totals from the line items
    lines_qs = AlabamaSalesLine.objects.all()

    # Apply salesman scope for Salesman + Alabama
    if hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Salesman' and getattr(request.user.role, 'company', 'Junaid') == 'Alabama':
        scope_q = alabama_salesman_scope_q(request.user, field='sales_employee')
        lines_qs = lines_qs.filter(scope_q)

    # Apply filters
    if document_type_filter and document_type_filter != 'All':
        lines_qs = lines_qs.filter(document_type=document_type_filter)

    if salesmen_filter:
        clean_salesmen = [s for s in salesmen_filter if s.strip()]
        if clean_salesmen:
            lines_qs = lines_qs.filter(sales_employee__in=clean_salesmen)

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
        lines_qs = lines_qs.filter(posting_date__gte=start_date)
    if end_date:
        lines_qs = lines_qs.filter(posting_date__lte=end_date)

    if total_range:
        # Filter by net_sales per document - need subquery
        from django.db.models import Subquery, OuterRef
        doc_totals = AlabamaSalesLine.objects.filter(
            document_type=OuterRef('document_type'),
            document_number=OuterRef('document_number'),
        ).values('document_type', 'document_number').annotate(
            total=Sum('net_sales')
        ).values('total')
        # Simpler: filter after aggregation - we'll do it in Python or use a different approach
        # For now, use a subquery to get doc_ids that match
        pass  # Skip total_range for now - complex to implement

    if q:
        lines_qs = lines_qs.filter(
            Q(document_number__icontains=q) |
            Q(customer__customer_name__icontains=q) |
            Q(customer__customer_code__icontains=q) |
            Q(sales_employee__icontains=q)
        )

    # Aggregate to document level
    from django.db.models import F
    docs_qs = (
        lines_qs.values('document_type', 'document_number', 'posting_date')
        .annotate(
            net_sales=Sum('net_sales'),
            gross_profit=Sum('gross_profit'),
            customer_name=F('customer__customer_name'),
            sales_employee=F('sales_employee'),
        )
        .order_by('-posting_date', '-document_number')
    )

    # Deduplicate - values can have multiple rows per doc (one per line with different customer_name - use first)
    # Actually values + annotate with customer_name gives one row per line. We need one row per document.
    # Use a different approach: group by document_type, document_number, get first customer for display
    from django.db.models import Min, Max
    docs_qs = (
        lines_qs.values('document_type', 'document_number', 'posting_date')
        .annotate(
            net_sales=Sum('net_sales'),
            gross_profit=Sum('gross_profit'),
        )
        .order_by('-posting_date', '-document_number')
    )

    # Get customer names - we need a separate query or subquery
    # For simplicity: customer is same for all lines in a doc typically. Use Min for consistency.
    doc_list = list(docs_qs)
    doc_keys = [(d['document_type'], d['document_number']) for d in doc_list]
    # Get customer for first line of each doc
    customer_map = {}
    for dt, dn in doc_keys:
        first = AlabamaSalesLine.objects.filter(
            document_type=dt, document_number=dn
        ).select_related('customer').first()
        if first:
            customer_map[(dt, dn)] = first.customer.customer_name

    for d in doc_list:
        d['customer_name'] = customer_map.get((d['document_type'], d['document_number']), '—')
        d['sales_employee'] = AlabamaSalesLine.objects.filter(
            document_type=d['document_type'], document_number=d['document_number']
        ).values_list('sales_employee', flat=True).first() or '—'

    # Paginate
    paginator = Paginator(doc_list, page_size)
    page_num = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_num)

    # Salesmen for filter (use same base as lines_qs for scope)
    salesmen_base = AlabamaSalesLine.objects.all()
    if hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Salesman' and getattr(request.user.role, 'company', 'Junaid') == 'Alabama':
        salesmen_base = salesmen_base.filter(alabama_salesman_scope_q(request.user, field='sales_employee'))
    salesmen = list(
        salesmen_base.exclude(sales_employee__isnull=True)
        .exclude(sales_employee='')
        .values_list('sales_employee', flat=True)
        .distinct()
        .order_by('sales_employee')
    )

    # Stats for filtered view
    today_lines = AlabamaSalesLine.objects.filter(posting_date=today)
    if hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Salesman' and getattr(request.user.role, 'company', 'Junaid') == 'Alabama':
        today_lines = today_lines.filter(alabama_salesman_scope_q(request.user, field='sales_employee'))
    if document_type_filter and document_type_filter != 'All':
        today_lines = today_lines.filter(document_type=document_type_filter)
    if salesmen_filter:
        clean = [s for s in salesmen_filter if s.strip()]
        if clean:
            today_lines = today_lines.filter(sales_employee__in=clean)
    if start_date:
        today_lines = today_lines.filter(posting_date__gte=start_date)
    if end_date:
        today_lines = today_lines.filter(posting_date__lte=end_date)

    today_sales = today_lines.aggregate(s=Sum('net_sales'))['s'] or 0
    today_gp = today_lines.aggregate(s=Sum('gross_profit'))['s'] or 0
    month_lines = AlabamaSalesLine.objects.filter(
        posting_date__year=current_year, posting_date__month=current_month
    )
    if hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Salesman' and getattr(request.user.role, 'company', 'Junaid') == 'Alabama':
        month_lines = month_lines.filter(alabama_salesman_scope_q(request.user, field='sales_employee'))
    if document_type_filter and document_type_filter != 'All':
        month_lines = month_lines.filter(document_type=document_type_filter)
    if salesmen_filter:
        clean = [s for s in salesmen_filter if s.strip()]
        if clean:
            month_lines = month_lines.filter(sales_employee__in=clean)
    if start_date:
        month_lines = month_lines.filter(posting_date__gte=start_date)
    if end_date:
        month_lines = month_lines.filter(posting_date__lte=end_date)
    month_sales = month_lines.aggregate(s=Sum('net_sales'))['s'] or 0
    month_gp = month_lines.aggregate(s=Sum('gross_profit'))['s'] or 0
    year_lines = AlabamaSalesLine.objects.filter(posting_date__year=current_year)
    if hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Salesman' and getattr(request.user.role, 'company', 'Junaid') == 'Alabama':
        year_lines = year_lines.filter(alabama_salesman_scope_q(request.user, field='sales_employee'))
    if document_type_filter and document_type_filter != 'All':
        year_lines = year_lines.filter(document_type=document_type_filter)
    if salesmen_filter:
        clean = [s for s in salesmen_filter if s.strip()]
        if clean:
            year_lines = year_lines.filter(sales_employee__in=clean)
    if start_date:
        year_lines = year_lines.filter(posting_date__gte=start_date)
    if end_date:
        year_lines = year_lines.filter(posting_date__lte=end_date)
    year_sales = year_lines.aggregate(s=Sum('net_sales'))['s'] or 0
    year_gp = year_lines.aggregate(s=Sum('gross_profit'))['s'] or 0

    # Totals for filtered page
    total_net_sales = sum(d['net_sales'] or 0 for d in doc_list)
    total_gp = sum(d['gross_profit'] or 0 for d in doc_list)

    # Build query string for pagination (exclude page)
    from urllib.parse import urlencode
    qd = request.GET.copy()
    if 'page' in qd:
        del qd['page']
    query_string = qd.urlencode()

    context = {
        'page_obj': page_obj,
        'total_count': paginator.count,
        'query_string': query_string,
        'filters': {
            'q': q,
            'salesmen_filter': salesmen_filter,
            'document_type': document_type_filter or 'All',
            'start': start,
            'end': end,
            'total': total_range,
            'page_size': page_size,
        },
        'salesmen': salesmen,
        'today_sales': today_sales,
        'today_gp': today_gp,
        'month_sales': month_sales,
        'month_gp': month_gp,
        'year_sales': year_sales,
        'year_gp': year_gp,
        'total_without_vat_value': total_net_sales,
        'total_gross_profit_value': total_gp,
        'is_admin': request.user.is_superuser or request.user.is_staff or (
            request.user.username or ''
        ).strip().lower() == 'manager',
        'active_page': 'sales_summary',
    }
    return render(request, 'alabama/sales_summary_list.html', context)


@login_required
def sales_summary_detail(request, doc_type_slug, document_number):
    """
    Detail view for a single Alabama document (Invoice or Credit Memo),
    similar to AR invoice detail in the Junaid app.
    """
    type_map = {
        'invoice': 'Invoice',
        'credit-memo': 'Credit Memo',
        'credit_memo': 'Credit Memo',
    }
    doc_type = type_map.get(doc_type_slug.lower())
    if not doc_type:
        raise Http404("Invalid document type")

    lines = (
        AlabamaSalesLine.objects
        .filter(document_type=doc_type, document_number=document_number)
        .select_related('customer', 'item')
        .order_by('id')
    )
    if not lines.exists():
        raise Http404("Document not found")

    header = lines.first()
    totals = lines.aggregate(
        total_quantity=Sum('quantity'),
        total_net_sales=Sum('net_sales'),
        total_gross_profit=Sum('gross_profit'),
    )

    context = {
        'doc_type': doc_type,
        'doc_type_slug': doc_type_slug,
        'document_number': document_number,
        'header': header,
        'lines': lines,
        'totals': totals,
        'active_page': 'sales_summary_detail',
    }
    return render(request, 'alabama/sales_summary_detail.html', context)


@login_required
def sales_summary_upload(request):
    """Upload Excel file for Alabama Sales Summary."""
    import pandas as pd
    from so.models import Customer, Items
    from django.db import transaction

    def _col_map(df):
        """Map column names to expected keys."""
        col_map = {}
        # canonical_key -> [aliases] (all lowercase)
        aliases = {
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

    def parse_date(val):
        if pd.isna(val):
            return None
        if hasattr(val, 'date'):
            return val.date()
        # Excel serial date (float)
        if isinstance(val, (int, float)):
            try:
                return pd.Timestamp(val).date()
            except Exception:
                pass
        s = str(val).strip()
        # DD.MM.YY (e.g. 12.01.26 = 12 Jan 2026), DD.MM.YYYY, YYYY-MM-DD, etc.
        for fmt in ['%d.%m.%y', '%d.%m.%Y', '%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%d-%m-%y', '%Y%m%d']:
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None

    def to_decimal(x):
        if pd.isna(x):
            return Decimal('0')
        try:
            return Decimal(str(x).replace(',', '').strip())
        except Exception:
            return Decimal('0')

    def to_str(x):
        if pd.isna(x):
            return ''
        s = str(x).strip()
        # Strip trailing .0 for integers (Excel converts numbers to float)
        if s.endswith('.0') and s.replace('.', '').isdigit():
            s = s[:-2]
        return s

    if request.method == 'POST':
        excel_file = request.FILES.get('excel_file')
        if not excel_file:
            messages.error(request, 'Please upload an Excel file.')
            return render(request, 'alabama/sales_summary_upload.html', {'active_page': 'sales_summary_upload'})

        try:
            df = pd.read_excel(excel_file)
            # Normalize column names: strip, remove BOM/non-breaking spaces
            df.columns = [str(c).strip().replace('\ufeff', '').replace('\xa0', ' ') for c in df.columns]
            col_map = _col_map(df)
            required = ['document_type', 'document_number', 'postingdate', 'customer_code', 'customer_name',
                        'sales_employee', 'itemcode', 'item_description', 'item_manufacturer',
                        'quantity', 'net_sales', 'gross_profit']
            missing = [r for r in required if r not in col_map.values()]
            if missing:
                return render(request, 'alabama/sales_summary_upload.html', {
                    'error': f'Missing columns: {", ".join(missing)}. Expected: Document Type, Document Number, PostingDate, Customer Code, Customer Name, Sales Employee, ItemCode, Item Description, Item Manufacturer, Quantity, Net Sales, Gross Profit',
                    'active_page': 'sales_summary_upload',
                })

            rev_map = {v: k for k, v in col_map.items() if v in required}
            rev_map = {k: rev_map[k] for k in required if k in rev_map}

            def get_val(row, key):
                col = rev_map.get(key)
                if col is None:
                    return ''
                return row.get(col, '')

            docs_to_replace = set()
            rows_to_insert = []

            for idx, row in df.iterrows():
                doc_type_raw = to_str(get_val(row, 'document_type'))
                if not doc_type_raw:
                    continue
                doc_type = 'Credit Memo' if 'credit' in doc_type_raw.lower() else 'Invoice'
                doc_no = to_str(get_val(row, 'document_number'))
                if not doc_no:
                    continue
                posting_d = parse_date(get_val(row, 'postingdate'))
                if not posting_d:
                    continue
                cust_code = to_str(get_val(row, 'customer_code'))
                cust_name = to_str(get_val(row, 'customer_name'))
                if not cust_code or not cust_name:
                    continue
                sales_emp = normalize_alabama_salesman(to_str(get_val(row, 'sales_employee')))
                item_code = to_str(get_val(row, 'itemcode'))
                item_desc = to_str(get_val(row, 'item_description'))
                item_manu = to_str(get_val(row, 'item_manufacturer'))

                if not item_code:
                    continue

                qty = to_decimal(get_val(row, 'quantity'))
                net_sales = to_decimal(get_val(row, 'net_sales'))
                gp = to_decimal(get_val(row, 'gross_profit'))

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
                    'document_number': doc_no,
                    'posting_date': posting_d,
                    'customer': customer,
                    'sales_employee': sales_emp or None,
                    'item': item,
                    'quantity': qty,
                    'net_sales': net_sales,
                    'gross_profit': gp,
                })

            if not rows_to_insert:
                return render(request, 'alabama/sales_summary_upload.html', {
                    'error': f'No valid rows found. Check: (1) Dates must be valid (Excel date or YYYY-MM-DD), (2) Document Number, Customer Code, Customer Name, Item Code must not be empty. Found {len(df)} rows in file. Detected columns: {list(col_map.keys())}',
                    'active_page': 'sales_summary_upload',
                })

            with transaction.atomic():
                for doc_type, doc_no in docs_to_replace:
                    AlabamaSalesLine.objects.filter(
                        document_type=doc_type, document_number=doc_no
                    ).delete()
                AlabamaSalesLine.objects.bulk_create([
                    AlabamaSalesLine(**r) for r in rows_to_insert
                ])

            messages.success(
                request,
                f'Successfully uploaded {len(rows_to_insert)} line items from {len(docs_to_replace)} documents.'
            )
            return redirect('alabama:settings')

        except Exception as e:
            messages.error(request, f'Upload failed: {str(e)}')
            return render(request, 'alabama/sales_summary_upload.html', {'error': str(e), 'active_page': 'sales_summary_upload'})

    return render(request, 'alabama/sales_summary_upload.html', {'active_page': 'sales_summary_upload'})


# =====================
# Purchase Summary
# =====================

@login_required
def purchase_summary_list(request):
    """Purchase Summary list - similar to Sales Summary."""
    today = date.today()
    current_year = today.year
    current_month = today.month

    q = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    document_type_filter = request.GET.get('document_type', '').strip()
    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()
    page_size = int(request.GET.get('page_size', 25)) or 25
    page_size = max(5, min(100, page_size))

    lines_qs = AlabamaPurchaseLine.objects.all()

    if hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Salesman' and getattr(request.user.role, 'company', 'Junaid') == 'Alabama':
        scope_q = alabama_salesman_scope_q(request.user, field='sales_employee')
        lines_qs = lines_qs.filter(scope_q)

    if document_type_filter and document_type_filter != 'All':
        lines_qs = lines_qs.filter(document_type__icontains=document_type_filter)

    if salesmen_filter:
        clean_salesmen = [s for s in salesmen_filter if s.strip()]
        if clean_salesmen:
            lines_qs = lines_qs.filter(sales_employee__in=clean_salesmen)

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
        lines_qs = lines_qs.filter(posting_date__gte=start_date)
    if end_date:
        lines_qs = lines_qs.filter(posting_date__lte=end_date)

    if q:
        lines_qs = lines_qs.filter(
            Q(document_number__icontains=q) |
            Q(vendor_name__icontains=q) |
            Q(vendor_code__icontains=q) |
            Q(sales_employee__icontains=q)
        )

    docs_qs = (
        lines_qs.values('document_type', 'document_number', 'posting_date')
        .annotate(
            net_purchase=Sum('net_purchase'),
        )
        .order_by('-posting_date', '-document_number')
    )

    doc_list = list(docs_qs)
    doc_keys = [(d['document_type'], d['document_number']) for d in doc_list]
    vendor_map = {}
    sales_emp_map = {}
    for dt, dn in doc_keys:
        first = AlabamaPurchaseLine.objects.filter(
            document_type=dt, document_number=dn
        ).first()
        if first:
            vendor_map[(dt, dn)] = first.vendor_name
            sales_emp_map[(dt, dn)] = first.sales_employee or '—'

    for d in doc_list:
        d['vendor_name'] = vendor_map.get((d['document_type'], d['document_number']), '—')
        d['sales_employee'] = sales_emp_map.get((d['document_type'], d['document_number']), '—')

    paginator = Paginator(doc_list, page_size)
    page_num = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_num)

    salesmen_base = AlabamaPurchaseLine.objects.all()
    if hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Salesman' and getattr(request.user.role, 'company', 'Junaid') == 'Alabama':
        salesmen_base = salesmen_base.filter(alabama_salesman_scope_q(request.user, field='sales_employee'))
    salesmen = list(
        salesmen_base.exclude(sales_employee__isnull=True)
        .exclude(sales_employee='')
        .values_list('sales_employee', flat=True)
        .distinct()
        .order_by('sales_employee')
    )

    today_lines = AlabamaPurchaseLine.objects.filter(posting_date=today)
    if hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Salesman' and getattr(request.user.role, 'company', 'Junaid') == 'Alabama':
        today_lines = today_lines.filter(alabama_salesman_scope_q(request.user, field='sales_employee'))
    if document_type_filter and document_type_filter != 'All':
        today_lines = today_lines.filter(document_type__icontains=document_type_filter)
    if salesmen_filter:
        clean = [s for s in salesmen_filter if s.strip()]
        if clean:
            today_lines = today_lines.filter(sales_employee__in=clean)
    if start_date:
        today_lines = today_lines.filter(posting_date__gte=start_date)
    if end_date:
        today_lines = today_lines.filter(posting_date__lte=end_date)
    today_purchase = today_lines.aggregate(s=Sum('net_purchase'))['s'] or 0

    month_lines = AlabamaPurchaseLine.objects.filter(
        posting_date__year=current_year, posting_date__month=current_month
    )
    if hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Salesman' and getattr(request.user.role, 'company', 'Junaid') == 'Alabama':
        month_lines = month_lines.filter(alabama_salesman_scope_q(request.user, field='sales_employee'))
    if document_type_filter and document_type_filter != 'All':
        month_lines = month_lines.filter(document_type__icontains=document_type_filter)
    if salesmen_filter:
        clean = [s for s in salesmen_filter if s.strip()]
        if clean:
            month_lines = month_lines.filter(sales_employee__in=clean)
    if start_date:
        month_lines = month_lines.filter(posting_date__gte=start_date)
    if end_date:
        month_lines = month_lines.filter(posting_date__lte=end_date)
    month_purchase = month_lines.aggregate(s=Sum('net_purchase'))['s'] or 0

    year_lines = AlabamaPurchaseLine.objects.filter(posting_date__year=current_year)
    if hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Salesman' and getattr(request.user.role, 'company', 'Junaid') == 'Alabama':
        year_lines = year_lines.filter(alabama_salesman_scope_q(request.user, field='sales_employee'))
    if document_type_filter and document_type_filter != 'All':
        year_lines = year_lines.filter(document_type__icontains=document_type_filter)
    if salesmen_filter:
        clean = [s for s in salesmen_filter if s.strip()]
        if clean:
            year_lines = year_lines.filter(sales_employee__in=clean)
    if start_date:
        year_lines = year_lines.filter(posting_date__gte=start_date)
    if end_date:
        year_lines = year_lines.filter(posting_date__lte=end_date)
    year_purchase = year_lines.aggregate(s=Sum('net_purchase'))['s'] or 0

    total_net_purchase = sum(d['net_purchase'] or 0 for d in doc_list)

    from urllib.parse import urlencode
    qd = request.GET.copy()
    if 'page' in qd:
        del qd['page']
    query_string = qd.urlencode()

    # Document types for filter (distinct from data)
    doc_types = list(
        AlabamaPurchaseLine.objects.values_list('document_type', flat=True).distinct().order_by('document_type')
    )

    context = {
        'page_obj': page_obj,
        'total_count': paginator.count,
        'query_string': query_string,
        'filters': {
            'q': q,
            'salesmen_filter': salesmen_filter,
            'document_type': document_type_filter or 'All',
            'start': start,
            'end': end,
            'page_size': page_size,
        },
        'salesmen': salesmen,
        'doc_types': doc_types,
        'today_purchase': today_purchase,
        'month_purchase': month_purchase,
        'year_purchase': year_purchase,
        'total_net_purchase': total_net_purchase,
        'is_admin': request.user.is_superuser or request.user.is_staff or (
            request.user.username or ''
        ).strip().lower() == 'manager',
        'active_page': 'purchase_summary',
    }
    return render(request, 'alabama/purchase_summary_list.html', context)


@login_required
def purchase_summary_detail(request, document_number):
    """Detail view for a single Purchase document."""
    lines = (
        AlabamaPurchaseLine.objects
        .filter(document_number=document_number)
        .select_related('item')
        .order_by('id')
    )
    if not lines.exists():
        raise Http404("Document not found")

    header = lines.first()
    totals = lines.aggregate(
        total_quantity=Sum('quantity'),
        total_net_purchase=Sum('net_purchase'),
    )

    context = {
        'document_number': document_number,
        'header': header,
        'lines': lines,
        'totals': totals,
        'active_page': 'purchase_summary_detail',
    }
    return render(request, 'alabama/purchase_summary_detail.html', context)


@login_required
def purchase_summary_upload(request):
    """Upload Excel file for Alabama Purchase Summary."""
    import pandas as pd
    from so.models import Items
    from django.db import transaction

    def _col_map(df):
        col_map = {}
        aliases = {
            'document_type': ['document type', 'documenttype', 'doc type'],
            'document_number': ['document number', 'documentnumber', 'doc no'],
            'document_date': ['document date', 'documentdate', 'posting date', 'postingdate'],
            'vendor_code': ['vendor code', 'vendorcode'],
            'vendor_name': ['vendor name', 'vendorname'],
            'sales_employee': ['sales employee', 'salesemployee', 'salesman'],
            'itemcode': ['itemcode', 'item code'],
            'item_description': ['item description', 'itemdescription'],
            'quantity': ['quantity', 'qty'],
            'unit_price': ['unitprice', 'unit price', 'price'],
            'item_manufacturer': ['item manufacturer', 'itemmanufacturer', 'manufacturer'],
            'net_purchase': ['net purchase', 'netpurchase'],
        }
        for col in df.columns:
            c = str(col).strip().lower().replace('\ufeff', '').replace('\xa0', ' ')
            for canonical, alis in aliases.items():
                if c == canonical or c in alis:
                    col_map[col] = canonical
                    break
        return col_map

    def parse_date(val):
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

    def to_decimal(x):
        if pd.isna(x):
            return Decimal('0')
        try:
            return Decimal(str(x).replace(',', '').strip())
        except Exception:
            return Decimal('0')

    def to_str(x):
        if pd.isna(x):
            return ''
        s = str(x).strip()
        if s.endswith('.0') and s.replace('.', '').isdigit():
            s = s[:-2]
        return s

    if request.method == 'POST':
        excel_file = request.FILES.get('excel_file')
        if not excel_file:
            messages.error(request, 'Please upload an Excel file.')
            return render(request, 'alabama/purchase_summary_upload.html', {'active_page': 'purchase_summary_upload'})

        try:
            df = pd.read_excel(excel_file)
            df.columns = [str(c).strip().replace('\ufeff', '').replace('\xa0', ' ') for c in df.columns]
            col_map = _col_map(df)
            required = ['document_type', 'document_number', 'document_date', 'vendor_code', 'vendor_name',
                       'sales_employee', 'itemcode', 'item_description', 'quantity', 'unit_price',
                       'item_manufacturer', 'net_purchase']
            missing = [r for r in required if r not in col_map.values()]
            if missing:
                return render(request, 'alabama/purchase_summary_upload.html', {
                    'error': f'Missing columns: {", ".join(missing)}. Expected: Document Type, Document Number, Document Date, Vendor Code, Vendor Name, Sales Employee, ItemCode, Item Description, Quantity, UnitPrice, Item Manufacturer, Net Purchase',
                    'active_page': 'purchase_summary_upload',
                })

            rev_map = {v: k for k, v in col_map.items() if v in required}
            rev_map = {k: rev_map[k] for k in required if k in rev_map}

            def get_val(row, key):
                col = rev_map.get(key)
                if col is None:
                    return ''
                return row.get(col, '')

            docs_to_replace = set()
            rows_to_insert = []

            for idx, row in df.iterrows():
                doc_type_raw = to_str(get_val(row, 'document_type'))
                if not doc_type_raw:
                    continue
                doc_no = to_str(get_val(row, 'document_number'))
                if not doc_no:
                    continue
                posting_d = parse_date(get_val(row, 'document_date'))
                if not posting_d:
                    continue
                vendor_code = to_str(get_val(row, 'vendor_code'))
                vendor_name = to_str(get_val(row, 'vendor_name'))
                if not vendor_code or not vendor_name:
                    continue
                sales_emp = normalize_alabama_salesman(to_str(get_val(row, 'sales_employee')))
                item_code = to_str(get_val(row, 'itemcode'))
                item_desc = to_str(get_val(row, 'item_description'))
                item_manu = to_str(get_val(row, 'item_manufacturer'))

                if not item_code:
                    continue

                qty = to_decimal(get_val(row, 'quantity'))
                unit_price = to_decimal(get_val(row, 'unit_price'))
                net_purchase = to_decimal(get_val(row, 'net_purchase'))

                docs_to_replace.add((doc_type_raw, doc_no))

                item, _ = Items.objects.get_or_create(
                    item_code=item_code,
                    defaults={
                        'item_description': item_desc or item_code,
                        'item_firm': item_manu or '',
                    }
                )

                rows_to_insert.append({
                    'document_type': doc_type_raw,
                    'document_number': doc_no,
                    'posting_date': posting_d,
                    'vendor_code': vendor_code,
                    'vendor_name': vendor_name,
                    'sales_employee': sales_emp or None,
                    'item': item,
                    'item_description': item_desc or None,
                    'item_manufacturer': item_manu or None,
                    'quantity': qty,
                    'unit_price': unit_price,
                    'net_purchase': net_purchase,
                })

            if not rows_to_insert:
                return render(request, 'alabama/purchase_summary_upload.html', {
                    'error': f'No valid rows found. Check: (1) Dates must be valid, (2) Document Number, Vendor Code, Vendor Name, Item Code must not be empty. Found {len(df)} rows. Detected columns: {list(col_map.keys())}',
                    'active_page': 'purchase_summary_upload',
                })

            with transaction.atomic():
                for doc_type, doc_no in docs_to_replace:
                    AlabamaPurchaseLine.objects.filter(
                        document_type=doc_type, document_number=doc_no
                    ).delete()
                AlabamaPurchaseLine.objects.bulk_create([
                    AlabamaPurchaseLine(**r) for r in rows_to_insert
                ])

            messages.success(
                request,
                f'Successfully uploaded {len(rows_to_insert)} line items from {len(docs_to_replace)} purchase documents.'
            )
            return redirect('alabama:settings')

        except Exception as e:
            messages.error(request, f'Upload failed: {str(e)}')
            return render(request, 'alabama/purchase_summary_upload.html', {'error': str(e), 'active_page': 'purchase_summary_upload'})

    return render(request, 'alabama/purchase_summary_upload.html', {'active_page': 'purchase_summary_upload'})


@login_required
def settings_page(request):
    """Alabama settings page with upload buttons and salesman name mappings."""
    if request.method == 'POST':
        action = request.POST.get('action', '')
        if action in ('add_mapping', 'delete_mapping') and (request.user.username or '').strip().lower() != 'manager':
            messages.error(request, 'Only manager can modify salesman mappings.')
            return redirect('alabama:settings')
        if action == 'add_mapping':
            raw = (request.POST.get('raw_name') or '').strip()
            norm = (request.POST.get('normalized_name') or '').strip()
            if raw and norm:
                existing = AlabamaSalesmanMapping.objects.filter(raw_name__iexact=raw).first()
                if existing:
                    existing.normalized_name = norm
                    existing.raw_name = raw
                    existing.save()
                else:
                    AlabamaSalesmanMapping.objects.create(raw_name=raw, normalized_name=norm)
                _clear_salesman_mapping_cache()
                messages.success(request, f'Added mapping: {raw} → {norm}')
            else:
                messages.error(request, 'Both "Variant" and "Maps to" are required.')
        elif action == 'delete_mapping':
            mid = request.POST.get('mapping_id')
            if mid:
                try:
                    m = AlabamaSalesmanMapping.objects.get(pk=mid)
                    m.delete()
                    _clear_salesman_mapping_cache()
                    messages.success(request, 'Mapping removed.')
                except AlabamaSalesmanMapping.DoesNotExist:
                    pass
        return redirect('alabama:settings')

    mappings = list(AlabamaSalesmanMapping.objects.all().order_by('raw_name'))
    # Only manager can see and manage mappings
    can_manage_mappings = (request.user.username or '').strip().lower() == 'manager'
    return render(request, 'alabama/settings.html', {
        'active_page': 'settings',
        'salesman_mappings': mappings,
        'can_manage_mappings': can_manage_mappings,
    })


# =====================
# Quotations
# =====================

@login_required
def quotation_list(request):
    """List Alabama SAP Quotations (Excel upload)."""
    from django.db.models.functions import Coalesce
    from django.db.models import Value, DecimalField

    qs = AlabamaSAPQuotation.objects.all()

    # Apply salesman scope for Salesman + Alabama
    if hasattr(request.user, 'role') and request.user.role and request.user.role.role == 'Salesman' and getattr(request.user.role, 'company', 'Junaid') == 'Alabama':
        scope_q = alabama_salesman_scope_q(request.user, field='salesman_name')
        qs = qs.filter(scope_q)

    q = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()
    status_filter = request.GET.get('status', '').strip()
    page_size = int(request.GET.get('page_size', 25)) or 25
    page_size = max(5, min(100, page_size))

    if salesmen_filter:
        clean = [s for s in salesmen_filter if s.strip()]
        if clean:
            qs = qs.filter(salesman_name__in=clean)

    if status_filter:
        sn = status_filter.upper()
        if sn in ['O', 'OPEN']:
            qs = qs.filter(status__in=['O', 'OPEN', 'Open', 'open'])
        elif sn in ['C', 'CLOSED']:
            qs = qs.filter(status__in=['C', 'CLOSED', 'Closed', 'closed'])
        else:
            qs = qs.filter(status__iexact=status_filter)

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
        qs = qs.filter(posting_date__gte=start_date)
    if end_date:
        qs = qs.filter(posting_date__lte=end_date)

    if q:
        if q.isdigit():
            qs = qs.filter(q_number__istartswith=q)
        elif len(q) < 3:
            qs = qs.filter(
                Q(customer_name__istartswith=q) |
                Q(salesman_name__istartswith=q)
            )
        else:
            qs = qs.filter(
                Q(q_number__icontains=q) |
                Q(customer_name__icontains=q) |
                Q(salesman_name__icontains=q)
            )

    total_value = qs.aggregate(
        total=Coalesce(Sum('document_total'), Value(0, output_field=DecimalField()))
    )['total'] or 0

    qs = qs.order_by('-posting_date', '-created_at')
    paginator = Paginator(qs, page_size)
    page_num = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_num)

    salesmen = list(
        AlabamaSAPQuotation.objects
        .exclude(salesman_name__isnull=True)
        .exclude(salesman_name='')
        .values_list('salesman_name', flat=True)
        .distinct()
        .order_by('salesman_name')
    )

    from urllib.parse import urlencode
    qd = request.GET.copy()
    if 'page' in qd:
        del qd['page']
    query_string = qd.urlencode()

    context = {
        'page_obj': page_obj,
        'total_count': paginator.count,
        'query_string': query_string,
        'total_value': total_value,
        'filters': {
            'q': q,
            'salesmen_filter': salesmen_filter,
            'status': status_filter,
            'start': start,
            'end': end,
            'page_size': page_size,
        },
        'salesmen': salesmen,
        'active_page': 'quotations',
    }
    return render(request, 'alabama/quotation_list.html', context)


@login_required
def quotation_detail(request, q_number):
    """Detail view for a single Alabama quotation."""
    quotation = AlabamaSAPQuotation.objects.filter(q_number=q_number).first()
    if not quotation:
        raise Http404("Quotation not found")

    items = list(quotation.items.all().order_by('id'))
    is_admin = request.user.is_superuser or request.user.is_staff or (
        request.user.username or ''
    ).strip().lower() == 'manager'

    total_estimated_cost = 0.0
    if is_admin and items:
        from so.models import Items
        item_codes = [str(i.item_no).strip() for i in items if i.item_no]
        if item_codes:
            cost_map = dict(
                Items.objects.filter(item_code__in=item_codes).values_list('item_code', 'item_cost')
            )
            for item in items:
                item.unit_cost = cost_map.get(str(item.item_no).strip() if item.item_no else '', 0.0)
                total_estimated_cost += float(item.unit_cost or 0) * float(item.quantity)
        else:
            for item in items:
                item.unit_cost = 0.0
    else:
        for item in items:
            item.unit_cost = None

    doc_total = float(quotation.document_total or 0)
    total_profit = doc_total - total_estimated_cost
    margin_percent = (total_profit / doc_total * 100) if doc_total else 0.0

    context = {
        'quotation': quotation,
        'items': items,
        'total_cost': total_estimated_cost,
        'total_profit': total_profit,
        'margin_percent': margin_percent,
        'is_admin': is_admin,
        'active_page': 'quotation_detail',
    }
    return render(request, 'alabama/quotation_detail.html', context)


@login_required
def quotation_upload(request):
    """Upload Excel file for Alabama SAP Quotations."""
    import pandas as pd
    from so.models import Customer, Items
    from django.db import transaction

    def _col_map(df):
        aliases = {
            'document_number': ['document number', 'documentnumber', 'doc no', 'q number'],
            'posting_date': ['posting date', 'postingdate'],
            'customer_code': ['customer/supplier no.', 'customer/supplier no', 'customer code', 'customercode', 'customer no'],
            'customer_name': ['customer/supplier name', 'customer/supplier name.', 'customer name', 'customername'],
            'salesman_name': ['sales employee name', 'sales employee name.', 'salesman', 'salesman name'],
            'manufacturer_name': ['manufacturer name', 'manufacturer name.', 'manufacturer', 'brand'],
            'bp_reference_no': ['bp reference no.', 'bp reference no', 'bp reference'],
            'item_no': ['item no.', 'item no', 'item code', 'itemcode'],
            'item_description': ['item/service description', 'item/service description.', 'item description', 'description'],
            'quantity': ['quantity', 'qty'],
            'price': ['price'],
            'row_total': ['row total'],
            'document_total': ['document total'],
            'status': ['status'],
            'bill_to': ['bill to'],
        }
        col_map = {}
        for col in df.columns:
            c = str(col).strip().lower().replace('\ufeff', '').replace('\xa0', ' ')
            for canonical, alis in aliases.items():
                if c == canonical or c in alis:
                    col_map[col] = canonical
                    break
        return col_map

    def parse_date(val):
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

    def to_decimal(x):
        if pd.isna(x):
            return Decimal('0')
        try:
            return Decimal(str(x).replace(',', '').strip())
        except Exception:
            return Decimal('0')

    def to_str(x):
        if pd.isna(x):
            return ''
        s = str(x).strip()
        if s.endswith('.0') and s.replace('.', '').isdigit():
            s = s[:-2]
        return s

    if request.method == 'POST':
        excel_file = request.FILES.get('excel_file')
        if not excel_file:
            messages.error(request, 'Please upload an Excel file.')
            return render(request, 'alabama/quotation_upload.html', {'active_page': 'quotation_upload'})

        try:
            df = pd.read_excel(excel_file)
            df.columns = [str(c).strip().replace('\ufeff', '').replace('\xa0', ' ') for c in df.columns]
            col_map = _col_map(df)
            # Core required - need at least item_no or item_description for line items
            required = [
                'document_number', 'posting_date', 'customer_name',
                'quantity', 'price', 'row_total',
            ]
            has_item = 'item_no' in col_map.values() or 'item_description' in col_map.values()
            if not has_item:
                missing = list(required) + ['item_no or item_description']
            else:
                missing = [r for r in required if r not in col_map.values()]
            if missing:
                return render(request, 'alabama/quotation_upload.html', {
                    'error': f'Missing columns: {", ".join(str(m) for m in missing)}. Expected: Document Number, Posting Date, Customer/Supplier No., Customer/Supplier Name, Sales Employee Name, Manufacturer Name, BP Reference No., Item No., Item/Service Description, Quantity, Price, Row Total, Document Total, Status, Bill To',
                    'active_page': 'quotation_upload',
                })

            all_keys = [
                'document_number', 'posting_date', 'customer_code', 'customer_name',
                'salesman_name', 'manufacturer_name', 'bp_reference_no',
                'item_no', 'item_description', 'quantity', 'price', 'row_total',
                'document_total', 'status', 'bill_to',
            ]
            rev_map = {v: k for k, v in col_map.items()}
            rev_map = {k: rev_map[k] for k in all_keys if k in rev_map}

            def get_val(row, key):
                col = rev_map.get(key)
                return row.get(col, '') if col else ''

            # Group rows by document_number to build header + items
            doc_rows = {}
            for idx, row in df.iterrows():
                doc_no = to_str(get_val(row, 'document_number'))
                if not doc_no:
                    continue
                posting_d = parse_date(get_val(row, 'posting_date'))
                if not posting_d:
                    continue
                item_no = to_str(get_val(row, 'item_no'))
                item_desc = to_str(get_val(row, 'item_description'))
                if not item_desc:
                    item_desc = item_no or '—'
                if not item_desc and not item_no:
                    continue  # Skip rows with no item info
                qty = to_decimal(get_val(row, 'quantity'))
                price = to_decimal(get_val(row, 'price'))
                row_total = to_decimal(get_val(row, 'row_total'))

                if doc_no not in doc_rows:
                    doc_rows[doc_no] = {
                        'posting_date': posting_d,
                        'customer_code': to_str(get_val(row, 'customer_code')),
                        'customer_name': to_str(get_val(row, 'customer_name')),
                        'salesman_name': normalize_alabama_salesman(to_str(get_val(row, 'salesman_name'))) or None,
                        'manufacturer_name': to_str(get_val(row, 'manufacturer_name')) or None,
                        'bp_reference_no': to_str(get_val(row, 'bp_reference_no')) or None,
                        'document_total': to_decimal(get_val(row, 'document_total')),
                        'status': to_str(get_val(row, 'status')) or None,
                        'bill_to': to_str(get_val(row, 'bill_to')) or None,
                        'items': [],
                    }
                doc_rows[doc_no]['items'].append({
                    'item_no': item_no or None,
                    'description': item_desc,
                    'quantity': qty,
                    'price': price,
                    'row_total': row_total,
                })

            if not doc_rows:
                return render(request, 'alabama/quotation_upload.html', {
                    'error': f'No valid rows found. Check dates and required fields. Found {len(df)} rows.',
                    'active_page': 'quotation_upload',
                })

            with transaction.atomic():
                for doc_no in doc_rows:
                    AlabamaSAPQuotation.objects.filter(q_number=doc_no).delete()
                for doc_no, data in doc_rows.items():
                    quot = AlabamaSAPQuotation.objects.create(
                        q_number=doc_no,
                        posting_date=data['posting_date'],
                        customer_code=data['customer_code'] or None,
                        customer_name=data['customer_name'] or '—',
                        salesman_name=data['salesman_name'],
                        brand=data['manufacturer_name'],
                        bp_reference_no=data['bp_reference_no'],
                        document_total=data['document_total'],
                        status=data['status'],
                        bill_to=data['bill_to'],
                    )
                    AlabamaSAPQuotationItem.objects.bulk_create([
                        AlabamaSAPQuotationItem(
                            quotation=quot,
                            item_no=it['item_no'],
                            description=it['description'],
                            quantity=it['quantity'],
                            price=it['price'],
                            row_total=it['row_total'],
                        )
                        for it in data['items']
                    ])

            messages.success(
                request,
                f'Successfully uploaded {len(doc_rows)} quotations.'
            )
            return redirect('alabama:settings')

        except Exception as e:
            messages.error(request, f'Upload failed: {str(e)}')
            return render(request, 'alabama/quotation_upload.html', {'error': str(e), 'active_page': 'quotation_upload'})

    return render(request, 'alabama/quotation_upload.html', {'active_page': 'quotation_upload'})
