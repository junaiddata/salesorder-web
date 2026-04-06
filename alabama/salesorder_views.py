"""
Alabama Sales Order views - list, detail, upload.
Stock resolved from so.Items at display time; show '-' if item not found.
"""
import pandas as pd
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_POST
from django.db.models import Q, Sum, Value, DecimalField, Exists, OuterRef, Case, When, CharField
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator
from django.db import transaction
from datetime import datetime, date
from decimal import Decimal

from .models import AlabamaSalesOrder, AlabamaSalesOrderItem
from .views import alabama_salesman_scope_q, normalize_alabama_salesman
from .telegram_remarks import can_send_alabama_remarks_telegram, send_alabama_remarks_to_salesman_telegram


def _norm_excel_header(s):
    return str(s).strip().lower().replace('\ufeff', '').replace('\xa0', ' ')


def _normalize_salesorder_customer_columns(df):
    """
    SAP exports may use Customer Code / Name, Customer/Supplier No., or Customer/Vendor Code / Name.
    Ensure standard columns exist by renaming known aliases (case-insensitive).
    """
    df = df.copy()
    df.columns = [str(c).strip().replace('\ufeff', '').replace('\xa0', ' ') for c in df.columns]
    key_to_col = {_norm_excel_header(c): c for c in df.columns}
    rename = {}
    if 'Customer/Supplier No.' not in df.columns:
        for a in (
            'customer/supplier no', 'customer/supplier no.', 'customer/supplier number',
            'customer/vendor code', 'customer vendor code',
            'customer code', 'customercode', 'customer no', 'customer no.',
        ):
            if a in key_to_col:
                rename[key_to_col[a]] = 'Customer/Supplier No.'
                break
    if 'Customer/Supplier Name' not in df.columns:
        for a in (
            'customer/supplier name', 'customer/supplier name.',
            'customer/vendor name', 'customer vendor name',
            'customer name', 'customername',
        ):
            if a in key_to_col:
                rename[key_to_col[a]] = 'Customer/Supplier Name'
                break
    if rename:
        df = df.rename(columns=rename)
    return df


def _can_edit_alabama_management_remarks(request):
    """Alabama Admin (company), manager username, Django staff/superuser."""
    if request.user.is_superuser or request.user.is_staff:
        return True
    if (request.user.username or '').strip().lower() == 'manager':
        return True
    role = getattr(request.user, 'role', None)
    if role and role.role == 'Admin' and getattr(role, 'company', '') == 'Alabama':
        return True
    return False


def _alabama_salesorder_detail_allowed(request, salesorder):
    return AlabamaSalesOrder.objects.filter(
        Q(pk=salesorder.pk) & alabama_salesman_scope_q(request.user, field='salesman_name')
    ).exists()


def _open_row_status_q() -> Q:
    """Return Q matching open line statuses (Open, O)."""
    return (
        Q(row_status__iexact="open")
        | Q(row_status__iexact="o")
        | Q(row_status__iexact="OPEN")
        | Q(row_status__iexact="O")
    )


@login_required
def salesorder_list(request):
    qs = AlabamaSalesOrder.objects.all().filter(alabama_salesman_scope_q(request.user, field='salesman_name'))

    open_items_sq = AlabamaSalesOrderItem.objects.filter(salesorder=OuterRef("pk")).filter(_open_row_status_q())
    qs = qs.annotate(
        has_open=Exists(open_items_sq),
        display_status=Case(
            When(has_open=True, then=Value("O")),
            default=Value("C"),
            output_field=CharField(),
        ),
        pending_total=Coalesce(
            Sum('items__pending_amount'),
            Value(0, output_field=DecimalField())
        ),
    )

    q = request.GET.get('q', '').strip()
    salesmen_filter = request.GET.getlist('salesman')
    start = request.GET.get('start', '').strip()
    end = request.GET.get('end', '').strip()
    status = request.GET.get('status', '').strip()
    total_range = request.GET.get('total', '').strip()
    remarks_filter = request.GET.get('remarks', '').strip()

    if salesmen_filter:
        clean_salesmen = [s for s in salesmen_filter if s.strip()]
        if clean_salesmen:
            qs = qs.filter(salesman_name__in=clean_salesmen)

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
        qs = qs.filter(management_remarks__isnull=False).exclude(management_remarks__exact="")
    elif remarks_filter == "NO":
        qs = qs.filter(Q(management_remarks__isnull=True) | Q(management_remarks__exact=""))

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

    if status:
        s = status.strip().upper()
        if s in ("OPEN", "O"):
            qs = qs.filter(has_open=True)
        elif s in ("CLOSED", "C"):
            qs = qs.filter(has_open=False)
        else:
            qs = qs.filter(status__iexact=status)

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

    grand_total_agg = qs.aggregate(
        total=Coalesce(Sum('items__pending_amount'), Value(0, output_field=DecimalField()))
    )
    total_value = grand_total_agg['total']

    qs = qs.order_by('-posting_date', '-so_number')

    try:
        page_size = int(request.GET.get('page_size', 100))
    except ValueError:
        page_size = 20
    page_size = max(5, min(page_size, 100))
    paginator = Paginator(qs, page_size)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    salesmen = (
        AlabamaSalesOrder.objects.filter(alabama_salesman_scope_q(request.user, field='salesman_name'))
        .exclude(salesman_name__isnull=True)
        .exclude(salesman_name='')
        .values_list('salesman_name', flat=True)
        .distinct()
        .order_by('salesman_name')
    )

    # Build query string for pagination (exclude 'page')
    get_copy = request.GET.copy()
    if 'page' in get_copy:
        del get_copy['page']
    query_string = get_copy.urlencode()

    return render(request, 'alabama/salesorder_list.html', {
        'page_obj': page_obj,
        'total_count': paginator.count,
        'salesmen': salesmen,
        'total_value': total_value,
        'query_string': query_string,
        'filters': {
            'q': q,
            'salesmen_filter': salesmen_filter,
            'status': status,
            'start': start,
            'end': end,
            'page_size': page_size,
            'total': total_range,
            'remarks': remarks_filter,
        },
    })


@login_required
def salesorder_detail(request, so_number):
    salesorder = get_object_or_404(AlabamaSalesOrder, so_number=so_number)

    if not _alabama_salesorder_detail_allowed(request, salesorder):
        raise Http404("Sales order not found")

    items = salesorder.items.all().order_by('line_no', 'id')

    # Stock from so.Items at display time; show '-' if item not found
    from so.models import Items
    item_codes = [str(it.item_no).strip() for it in items if it.item_no]
    stock_data = {}
    if item_codes:
        for row in Items.objects.filter(item_code__in=item_codes).values_list(
            'item_code', 'total_available_stock', 'dip_warehouse_stock'
        ):
            code, total, dip = row
            stock_data[str(code).strip() if code else ''] = {'total': total, 'dip': dip}

    # Attach stock to each item for template
    items_list = list(items)
    for it in items_list:
        key = str(it.item_no or '').strip()
        sd = stock_data.get(key, {})
        it.stock_total = sd.get('total')
        it.stock_dip = sd.get('dip')

    can_edit_comments = _can_edit_alabama_management_remarks(request)
    can_send_telegram = can_edit_comments and can_send_alabama_remarks_telegram(salesorder)

    return render(request, 'alabama/salesorder_detail.html', {
        'salesorder': salesorder,
        'items': items_list,
        'can_edit_management_remarks': can_edit_comments,
        'can_send_remarks_telegram': can_send_telegram,
    })


@login_required
@require_POST
def salesorder_update_remarks(request, so_number):
    salesorder = get_object_or_404(AlabamaSalesOrder, so_number=so_number)
    if not _alabama_salesorder_detail_allowed(request, salesorder):
        raise Http404("Sales order not found")
    if not _can_edit_alabama_management_remarks(request):
        messages.error(request, "You do not have permission to edit management remarks.")
        return redirect('alabama:salesorder_detail', so_number=salesorder.so_number)

    text = (request.POST.get('management_remarks') or '').strip()
    salesorder.management_remarks = text or None
    salesorder.save(update_fields=['management_remarks'])
    messages.success(request, "Management remarks updated.")
    return redirect('alabama:salesorder_detail', so_number=salesorder.so_number)


@login_required
@require_POST
def salesorder_send_remarks_telegram(request, so_number):
    """POST: save remarks and notify salesman's Telegram group (JSON)."""
    salesorder = get_object_or_404(AlabamaSalesOrder, so_number=so_number)
    if not _alabama_salesorder_detail_allowed(request, salesorder):
        return JsonResponse({'success': False, 'error': 'Not found'}, status=404)
    if not _can_edit_alabama_management_remarks(request):
        return JsonResponse({'success': False, 'error': 'Permission denied'}, status=403)

    remark_text = (request.POST.get('management_remarks') or '').strip() or (salesorder.management_remarks or '')
    salesorder.management_remarks = remark_text
    salesorder.save(update_fields=['management_remarks'])

    ok, err = send_alabama_remarks_to_salesman_telegram(salesorder, remark_text)
    if ok:
        return JsonResponse({'success': True})
    return JsonResponse({'success': False, 'error': err or 'Unknown error'}, status=400)


@login_required
def salesorder_upload(request):
    messages_list = []
    if request.method == 'POST':
        excel_file = request.FILES.get('excel_file')
        if not excel_file:
            messages_list.append('Please upload a file.')
        else:
            try:
                required_cols = [
                    'Document Number', 'Posting Date', 'BP Reference No.',
                    'Customer/Supplier No.', 'Customer/Supplier Name',
                    'Row Status', 'Job Type',
                    'Item No.', 'Item/Service Description', 'Manufacture',
                    'Quantity', 'Row Total',
                    'Remaining Open Quantity', 'Pending Amount',
                    'Sales Employee',
                ]

                df = pd.read_excel(excel_file)
                df = _normalize_salesorder_customer_columns(df)

                missing = [c for c in required_cols if c not in df.columns]
                if missing:
                    messages_list.append(f"Missing columns: {', '.join(missing)}")
                else:
                    cols_to_keep = required_cols.copy()
                    for opt in ['Discount', 'VAT Number', 'Remarks', 'Unit Price', 'Document Total(incl)', 'Overall Discount Amount']:
                        if opt in df.columns:
                            cols_to_keep.append(opt)
                    df = df[[c for c in cols_to_keep if c in df.columns]].copy()

                    def _clean_str_series(s):
                        s = s.astype("string")
                        s = s.fillna("")
                        s = s.str.strip()
                        s = s.str.replace(r"\.0$", "", regex=True)
                        return s

                    df["so_number"] = _clean_str_series(df["Document Number"])
                    df["customer_code"] = _clean_str_series(df["Customer/Supplier No."])
                    df["customer_name"] = _clean_str_series(df["Customer/Supplier Name"])
                    df["bp_reference_no"] = _clean_str_series(df["BP Reference No."])
                    raw_salesman = _clean_str_series(df["Sales Employee"])
                    df["salesman_name"] = raw_salesman.apply(lambda x: normalize_alabama_salesman(x) or x)
                    df["row_status_norm"] = _clean_str_series(df["Row Status"]).str.upper()
                    df["item_no"] = _clean_str_series(df["Item No."])
                    df["description"] = _clean_str_series(df["Item/Service Description"])
                    df["job_type"] = _clean_str_series(df["Job Type"])
                    df["manufacture"] = _clean_str_series(df["Manufacture"])

                    df["posting_date"] = pd.to_datetime(df["Posting Date"], errors="coerce", dayfirst=True).dt.date

                    def _num(col):
                        return pd.to_numeric(df[col], errors="coerce")

                    df["quantity_n"] = _num("Quantity").fillna(0)
                    df["row_total_n"] = _num("Row Total").fillna(0)
                    df["remaining_open_quantity_n"] = _num("Remaining Open Quantity")
                    df["pending_amount_n"] = _num("Pending Amount")

                    if "Unit Price" in df.columns:
                        df["price_n"] = _num("Unit Price").fillna(0)
                    else:
                        df["price_n"] = 0.0

                    if "Discount" in df.columns:
                        df["discount_n"] = _num("Discount").fillna(0)
                    else:
                        df["discount_n"] = pd.Series([0] * len(df), dtype=float)

                    if "Remarks" in df.columns:
                        df["remarks_clean"] = _clean_str_series(df["Remarks"])
                    else:
                        df["remarks_clean"] = pd.Series([""] * len(df), dtype=str)

                    df["is_open_line"] = df["row_status_norm"].isin(["OPEN", "O"])
                    df["line_no"] = df.groupby("so_number", sort=False).cumcount() + 1

                    agg_dict = {
                        "posting_date": ("posting_date", "first"),
                        "customer_code": ("customer_code", "first"),
                        "customer_name": ("customer_name", "first"),
                        "bp_reference_no": ("bp_reference_no", "first"),
                        "salesman_name": ("salesman_name", "first"),
                        "discount_percentage": ("discount_n", "first"),
                        "pending_total": ("pending_amount_n", "sum"),
                        "row_total_sum": ("row_total_n", "sum"),
                        "has_open": ("is_open_line", "any"),
                        "remarks": ("remarks_clean", "first"),
                    }
                    if "VAT Number" in df.columns:
                        df["vat_number_clean"] = _clean_str_series(df["VAT Number"])
                        agg_dict["vat_number"] = ("vat_number_clean", "first")

                    header_df = df.groupby("so_number", dropna=False).agg(**agg_dict).reset_index()

                    def _dec2(x):
                        try:
                            if x is None or (isinstance(x, float) and pd.isna(x)):
                                return Decimal("0.00")
                            return Decimal(str(x)).quantize(Decimal("0.01"))
                        except Exception:
                            return Decimal("0.00")

                    so_numbers = header_df["so_number"].tolist()

                    with transaction.atomic():
                        existing_map = {o.so_number: o for o in AlabamaSalesOrder.objects.filter(so_number__in=so_numbers)}
                        to_create = []
                        to_update = []

                        for row in header_df.itertuples(index=False):
                            so_no = row.so_number
                            status_val = "O" if bool(row.has_open) else "C"
                            remarks_val = (getattr(row, 'remarks', None) or "").strip()
                            if remarks_val and remarks_val.lower() in ('nan', 'none'):
                                remarks_val = ""
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
                                "remarks": remarks_val or None,
                            }
                            if hasattr(row, 'vat_number') and row.vat_number:
                                vat_str = str(row.vat_number).strip()
                                if vat_str.lower() in ('nan', 'none', ''):
                                    defaults["vat_number"] = ""
                                else:
                                    if vat_str.endswith('.0'):
                                        vat_str = vat_str[:-2]
                                    defaults["vat_number"] = vat_str
                            else:
                                defaults["vat_number"] = ""

                            obj = existing_map.get(so_no)
                            if obj is None:
                                to_create.append(AlabamaSalesOrder(so_number=so_no, **defaults))
                            else:
                                for k, v in defaults.items():
                                    setattr(obj, k, v)
                                to_update.append(obj)

                        if to_create:
                            AlabamaSalesOrder.objects.bulk_create(to_create, batch_size=2000)
                        if to_update:
                            update_fields = [
                                "posting_date", "customer_code", "customer_name", "bp_reference_no",
                                "salesman_name", "discount_percentage", "document_total", "row_total_sum",
                                "status", "remarks", "vat_number",
                            ]
                            AlabamaSalesOrder.objects.bulk_update(to_update, fields=update_fields, batch_size=2000)

                        order_id_map = dict(
                            AlabamaSalesOrder.objects.filter(so_number__in=so_numbers).values_list("so_number", "id")
                        )
                        AlabamaSalesOrderItem.objects.filter(salesorder__so_number__in=so_numbers).delete()

                        items_to_create = []

                        def _dec_any(x):
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
                                AlabamaSalesOrderItem(
                                    salesorder_id=so_id,
                                    line_no=int(getattr(r, 'line_no', 1)),
                                    item_no=r.item_no or "",
                                    description=r.description or "",
                                    quantity=_dec_any(r.quantity_n),
                                    price=_dec_any(getattr(r, 'price_n', 0)),
                                    row_total=_dec_any(r.row_total_n),
                                    row_status=(r.row_status_norm or ""),
                                    job_type=r.job_type or "",
                                    manufacture=r.manufacture or "",
                                    remaining_open_quantity=_dec_any(r.remaining_open_quantity_n),
                                    pending_amount=_dec_any(r.pending_amount_n),
                                )
                            )
                            if len(items_to_create) >= 10000:
                                AlabamaSalesOrderItem.objects.bulk_create(items_to_create, batch_size=20000)
                                items_to_create = []

                        if items_to_create:
                            AlabamaSalesOrderItem.objects.bulk_create(items_to_create, batch_size=10000)

                    messages.success(
                        request,
                        f"Imported {len(so_numbers)} sales orders and {len(df)} lines successfully."
                    )
                    return redirect('alabama:salesorder_list')
            except Exception as e:
                messages_list.append(str(e))

    return render(request, 'alabama/salesorder_upload.html', {'messages': messages_list})
