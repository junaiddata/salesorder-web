"""
Combined Alabama quotations: merges AlabamaSAPQuotation (Excel upload) with
in-app Quotation rows where division='ALABAMA'. Mirrors so.views_quotation's
combined_quotations_list, but self-contained since Alabama's SAP quotations
live in their own model/app (no Junaid division heuristics needed here).
"""
import calendar as _calendar
from datetime import date as date_cls
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_GET

from .models import AlabamaSAPQuotation, AlabamaSAPQuotationItem
from .views import alabama_salesman_scope_q


def _combined_salesman_pick_list(request):
    out = []
    for s in request.GET.getlist('salesman_filter'):
        t = (s or '').strip()
        if t and t != 'All':
            out.append(t)
    return out


def _is_salesman_role(request):
    return (
        request.user.is_authenticated
        and hasattr(request.user, 'role')
        and request.user.role.role == 'Salesman'
    )


def _alabama_combined_salesman_choices():
    from so.models import Quotation

    sap_names = set(
        AlabamaSAPQuotation.objects.exclude(salesman_name__isnull=True)
        .exclude(salesman_name='')
        .values_list('salesman_name', flat=True)
        .distinct()
    )
    app_names = set(
        Quotation.objects.filter(division='ALABAMA')
        .exclude(salesman__isnull=True)
        .values_list('salesman__salesman_name', flat=True)
        .distinct()
    )
    names = {(n or '').strip() for n in (sap_names | app_names) if n and str(n).strip()}
    return sorted(names, key=lambda x: (x.casefold(), x))


def alabama_sap_quotations_filtered_qs(request):
    """AlabamaSAPQuotation queryset with salesman scope + combined-list GET filters."""
    qs = AlabamaSAPQuotation.objects.filter(alabama_salesman_scope_q(request.user, field='salesman_name'))

    start_date = (request.GET.get('start_date') or '').strip()
    end_date = (request.GET.get('end_date') or '').strip()
    status = (request.GET.get('status') or 'All').strip()
    q = (request.GET.get('q') or '').strip()
    salesmen_pick = _combined_salesman_pick_list(request)

    if _is_salesman_role(request):
        pass
    elif salesmen_pick:
        sm_q = Q()
        for name in salesmen_pick:
            sm_q |= Q(salesman_name__iexact=name)
        qs = qs.filter(sm_q)

    if status and status != 'All':
        if status in ('Pending', 'On Hold'):
            qs = qs.filter(status__in=['O', 'OPEN', 'Open', 'open'])
        # App-only "Approved" has no SAP equivalent; leave SAP unfiltered by document status.

    if start_date:
        qs = qs.filter(posting_date__gte=start_date)
    if end_date:
        qs = qs.filter(posting_date__lte=end_date)

    if q:
        if q.isdigit():
            qs = qs.filter(q_number__istartswith=q)
        elif len(q) < 3:
            qs = qs.filter(Q(customer_name__istartswith=q) | Q(salesman_name__istartswith=q))
        else:
            qs = qs.filter(
                Q(q_number__icontains=q)
                | Q(customer_code__icontains=q)
                | Q(customer_name__icontains=q)
                | Q(salesman_name__icontains=q)
            )
    return qs


def alabama_inapp_quotations_filtered_qs(request):
    """In-app Quotation queryset (division=ALABAMA) with salesman scope + combined-list GET filters."""
    from so.models import Quotation
    from so.views import SALES_USER_MAP

    quotations = Quotation.objects.filter(division='ALABAMA').select_related('customer', 'salesman')

    start_date = (request.GET.get('start_date') or '').strip()
    end_date = (request.GET.get('end_date') or '').strip()
    status = (request.GET.get('status') or 'All').strip()
    q = (request.GET.get('q') or '').strip()
    salesmen_pick = _combined_salesman_pick_list(request)

    if status and status != 'All':
        quotations = quotations.filter(status=status)

    if _is_salesman_role(request):
        current_username = (request.user.username or '').strip().lower()
        allowed_names = SALES_USER_MAP.get(current_username)
        if allowed_names:
            quotations = quotations.filter(salesman__salesman_name__in=allowed_names)
        else:
            quotations = quotations.none()
    elif salesmen_pick:
        sm_q = Q()
        for name in salesmen_pick:
            sm_q |= Q(salesman__salesman_name__iexact=name)
        quotations = quotations.filter(sm_q)

    if start_date:
        quotations = quotations.filter(quotation_date__gte=start_date)
    if end_date:
        quotations = quotations.filter(quotation_date__lte=end_date)

    if q:
        quotations = quotations.filter(
            Q(quotation_number__icontains=q)
            | Q(customer__customer_name__icontains=q)
            | Q(customer__customer_code__icontains=q)
            | Q(salesman__salesman_name__icontains=q)
            | Q(remarks__icontains=q)
            | Q(customer_display_name__icontains=q)
        )
    return quotations.order_by('-created_at')


def _combined_sort_date(s):
    from datetime import date as date_cls
    from datetime import datetime as dt_cls
    if not s:
        return date_cls(1900, 1, 1)
    try:
        return dt_cls.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        return date_cls(1900, 1, 1)


def _combined_list_export_source(request):
    source = (request.GET.get('source') or 'all').strip().lower()
    if source not in ('all', 'sap', 'app'):
        source = 'all'
    return source


def _alabama_combined_quotation_calendar_context(request):
    """
    Month-calendar data for the Alabama combined quotations page: merges SAP (posting_date)
    and Alabama in-app (quotation_date) quotations, aggregated per day into total value / GP /
    count. Respects the combined list filters but ignores the list start/end date range.
    """
    import copy
    from so.models import Items

    today = date_cls.today()
    current_year = today.year
    current_month = today.month

    calendar_year_min = 2024
    calendar_year_max = current_year + 1
    calendar_years = list(range(calendar_year_min, calendar_year_max + 1))
    try:
        qcal_year = int((request.GET.get('qcal_year') or '').strip())
    except (ValueError, TypeError):
        qcal_year = current_year
    if qcal_year not in calendar_years:
        qcal_year = current_year

    qcal_months = []
    for raw in request.GET.getlist('qcal_month'):
        try:
            m = int(str(raw).strip())
            if 1 <= m <= 12:
                qcal_months.append(m)
        except (ValueError, TypeError):
            continue
    qcal_months = sorted(set(qcal_months))
    if not qcal_months:
        qcal_months = [current_month]

    month_names_short = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    calendar_months = [(i, month_names_short[i - 1]) for i in range(1, 13)]
    if len(qcal_months) == 1:
        calendar_period_label = date_cls(qcal_year, qcal_months[0], 1).strftime('%B %Y')
    else:
        calendar_period_label = ', '.join(_calendar.month_name[m] for m in qcal_months) + ' ' + str(qcal_year)

    source = _combined_list_export_source(request)

    cal_request = copy.copy(request)
    qd = request.GET.copy()
    qd.pop('start_date', None)
    qd.pop('end_date', None)
    cal_request.GET = qd

    day_data = {}

    def _bump(d, value, gp):
        if not d:
            return
        cell = day_data.get(d)
        if cell is None:
            cell = {'total_value': Decimal('0'), 'gp': Decimal('0'), 'count': 0}
            day_data[d] = cell
        cell['total_value'] += value
        cell['gp'] += gp
        cell['count'] += 1

    if source in ('all', 'sap'):
        sap_list = list(
            alabama_sap_quotations_filtered_qs(cal_request)
            .filter(posting_date__year=qcal_year, posting_date__month__in=qcal_months)
            .prefetch_related('items')
        )
        sap_codes = {
            str(it.item_no).strip()
            for qn in sap_list for it in qn.items.all()
            if it.item_no and str(it.item_no).strip()
        }
        cost_map = dict(Items.objects.filter(item_code__in=sap_codes).values_list('item_code', 'item_cost'))
        for qn in sap_list:
            total = Decimal(str(qn.document_total or 0))
            cost = Decimal('0')
            for it in qn.items.all():
                code = str(it.item_no).strip() if it.item_no else ''
                cost += Decimal(str(cost_map.get(code) or 0)) * Decimal(str(it.quantity or 0))
            _bump(qn.posting_date, total, total - cost)

    if source in ('all', 'app'):
        app_list = list(
            alabama_inapp_quotations_filtered_qs(cal_request)
            .filter(quotation_date__year=qcal_year, quotation_date__month__in=qcal_months)
            .prefetch_related('items__item')
        )
        for qn in app_list:
            total = Decimal(str(qn.grand_total or 0))
            cost = Decimal('0')
            for it in qn.items.all():
                if it.item_id and it.item.item_cost is not None:
                    cost += Decimal(str(it.item.item_cost)) * Decimal(str(it.quantity or 0))
            _bump(qn.quotation_date, total, total - cost)

    month_days = []
    weekly_totals = []
    for grid_month in qcal_months:
        _, last_day_in_month = _calendar.monthrange(qcal_year, grid_month)
        is_current_month = (qcal_year == current_year and grid_month == current_month)
        last_day = min(today.day, last_day_in_month) if is_current_month else last_day_in_month
        week_acc = None
        for day_num in range(1, last_day + 1):
            day_date = date_cls(qcal_year, grid_month, day_num)
            cell = day_data.get(day_date)
            day_total = cell['total_value'] if cell else Decimal('0')
            day_gp = cell['gp'] if cell else Decimal('0')
            day_count = cell['count'] if cell else 0

            if day_date.weekday() == 0 or week_acc is None:
                week_acc = {
                    'label': f"Week of {day_date.strftime('%d %b')}",
                    'total_value': Decimal('0'),
                    'gp': Decimal('0'),
                    'quotation_count': 0,
                }
                weekly_totals.append(week_acc)
            if day_date.weekday() <= 5:
                week_acc['total_value'] += day_total
                week_acc['gp'] += day_gp
                week_acc['quotation_count'] += day_count

            month_days.append({
                'date': day_date,
                'formatted_date': f"{month_names_short[grid_month - 1]} {day_num}",
                'total_value': day_total,
                'gp': day_gp,
                'quotation_count': day_count,
                'has_quotations': bool(cell and cell['count'] > 0),
            })

    for week in weekly_totals:
        _wv = float(week['total_value'])
        week['gp_pct'] = round(float(week['gp']) / _wv * 100, 1) if _wv else 0

    return {
        'today': today,
        'qcal_year': qcal_year,
        'qcal_months': qcal_months,
        'calendar_years': calendar_years,
        'calendar_months': calendar_months,
        'calendar_period_label': calendar_period_label,
        'combined_month_days': month_days,
        'combined_weekly_totals': weekly_totals,
        'combined_month_total_value': sum((d['total_value'] for d in month_days), Decimal('0')),
        'combined_month_total_gp': sum((d['gp'] for d in month_days), Decimal('0')),
        'combined_month_quotation_count': sum(d['quotation_count'] for d in month_days),
    }


@login_required
def combined_quotations_list(request):
    """
    Single list merging Alabama SAP quotations (posting_date, Excel upload) and Alabama
    in-app quotations (quotation_date, division=ALABAMA), sorted by date descending.
    source=all|sap|app.
    """
    if request.GET.get('ajax') == 'combined_calendar' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        cal_ctx = _alabama_combined_quotation_calendar_context(request)
        calendar_html = render_to_string(
            'alabama/quotations/_combined_calendar_body.html',
            {
                'month_days': cal_ctx['combined_month_days'],
                'weekly_totals': cal_ctx['combined_weekly_totals'],
                'today': cal_ctx['today'],
                'qcal_months': cal_ctx['qcal_months'],
                'combined_month_total_value': cal_ctx['combined_month_total_value'],
                'combined_month_total_gp': cal_ctx['combined_month_total_gp'],
                'combined_month_quotation_count': cal_ctx['combined_month_quotation_count'],
            },
            request=request,
        )
        return HttpResponse(calendar_html, content_type='text/html')

    source = _combined_list_export_source(request)

    rows = []
    if source in ('all', 'sap'):
        sap_qs = alabama_sap_quotations_filtered_qs(request).order_by('-posting_date', '-id')
        for s in sap_qs.iterator(chunk_size=500):
            rows.append({
                'display_date': s.posting_date,
                'source': 'SAP',
                'number': s.q_number,
                'customer_code': s.customer_code or '',
                'customer_name': s.customer_name or '',
                'total': float(s.document_total or 0),
                'status': (s.status or '')[:80],
                'salesman': s.salesman_name or '',
                'detail_url': reverse('alabama:quotation_detail', args=[s.q_number]),
            })
    if source in ('all', 'app'):
        app_qs = alabama_inapp_quotations_filtered_qs(request).order_by('-quotation_date', '-id')
        for a in app_qs.iterator(chunk_size=500):
            cust = a.customer
            name = a.customer_display_name or (cust.customer_name if cust else '')
            rows.append({
                'display_date': a.quotation_date,
                'source': 'APP',
                'number': a.quotation_number or '',
                'customer_code': cust.customer_code if cust else '',
                'customer_name': name,
                'total': float(a.grand_total or 0),
                'status': (a.status or '')[:80],
                'salesman': a.salesman.salesman_name if a.salesman_id else '',
                'detail_url': reverse('view_quotation_details', args=[a.id]),
            })

    _min_date = date_cls(1900, 1, 1)
    rows.sort(key=lambda r: ((r['display_date'] or _min_date), r.get('number') or ''), reverse=True)

    page = request.GET.get('page', 1)
    try:
        page_size = int(request.GET.get('page_size', 50))
    except ValueError:
        page_size = 50
    page_size = max(10, min(page_size, 200))

    paginator = Paginator(rows, page_size)
    try:
        page_obj = paginator.page(page)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    qd = request.GET.copy()
    qd.pop('page', None)
    query_string = qd.urlencode()

    calendar_ctx = _alabama_combined_quotation_calendar_context(request)

    return render(request, 'alabama/quotations/combined_quotations.html', {
        'page_obj': page_obj,
        'total_count': paginator.count,
        'query_string': query_string,
        'salesman_choices': _alabama_combined_salesman_choices(),
        **calendar_ctx,
        'selected_salesmen': _combined_salesman_pick_list(request),
        'start_date': request.GET.get('start_date', ''),
        'end_date': request.GET.get('end_date', ''),
        'search_query': request.GET.get('q', ''),
        'current_status': request.GET.get('status', 'All'),
        'source': source,
        'page_size': page_size,
        'active_page': 'combined_quotations',
    })


def _style_combined_excel_worksheet(worksheet):
    from openpyxl.styles import Font, PatternFill, Alignment

    header_fill = PatternFill(start_color="c91f16", end_color="c91f16", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    body_align_wrap = Alignment(vertical="center", wrap_text=True)
    for cell in worksheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for column in worksheet.columns:
        col_letter = column[0].column_letter
        header_title = str(column[0].value or "")
        max_length = len(header_title)
        for cell in column:
            if cell.row == 1:
                continue
            try:
                if cell.value is not None:
                    max_length = max(max_length, len(str(cell.value)))
            except Exception:
                pass
            if header_title in ('Price', 'Total Value', 'Quantity'):
                cell.alignment = Alignment(vertical="center", horizontal="right")
            else:
                cell.alignment = body_align_wrap
        if header_title == 'Source':
            adjusted_width = max(max_length + 2, 11)
        elif header_title in ('Customer Name', 'Item Name'):
            adjusted_width = min(max(max_length + 4, 28), 55)
        elif header_title in ('Price', 'Total Value'):
            adjusted_width = min(max(max_length + 4, 16), 22)
        elif header_title == 'Quantity':
            adjusted_width = min(max(max_length + 3, 14), 18)
        elif header_title == 'Date':
            adjusted_width = max(max_length + 2, 14)
        else:
            adjusted_width = min(max(max_length + 3, 16), 48)
        worksheet.column_dimensions[col_letter].width = adjusted_width

    worksheet.row_dimensions[1].height = 24
    worksheet.freeze_panes = "A2"


@login_required
@require_GET
def export_combined_quotations_consolidated_excel(request):
    """Combined Alabama SAP + in-app quotations, one row per quote; same filters as combined list."""
    import pandas as pd
    from io import BytesIO
    from django.utils import timezone

    source = _combined_list_export_source(request)
    rows = []
    if source in ('all', 'sap'):
        sap_qs = alabama_sap_quotations_filtered_qs(request).order_by('-posting_date', '-id')
        for s in sap_qs.iterator(chunk_size=500):
            d = s.posting_date
            rows.append({
                'Source': 'SAP',
                'Quotation Number': s.q_number,
                'Date': d.strftime('%Y-%m-%d') if d else '',
                'Customer Code': s.customer_code or '',
                'Customer Name': s.customer_name or '',
                'Total Value': float(s.document_total or 0),
                'Status': (s.status or ''),
                'Salesman': s.salesman_name or '',
            })
    if source in ('all', 'app'):
        app_qs = alabama_inapp_quotations_filtered_qs(request).order_by('-quotation_date', '-id')
        for a in app_qs.iterator(chunk_size=500):
            cust = a.customer
            d = a.quotation_date
            name = a.customer_display_name or (cust.customer_name if cust else '')
            rows.append({
                'Source': 'App',
                'Quotation Number': a.quotation_number or '',
                'Date': d.strftime('%Y-%m-%d') if d else '',
                'Customer Code': cust.customer_code if cust else '',
                'Customer Name': name,
                'Total Value': float(a.grand_total or 0),
                'Status': (a.status or ''),
                'Salesman': a.salesman.salesman_name if a.salesman_id else '',
            })
    rows.sort(
        key=lambda r: (_combined_sort_date(r['Date']), r.get('Quotation Number') or '', r.get('Source') or ''),
        reverse=True,
    )
    cols = ['Source', 'Quotation Number', 'Date', 'Customer Code', 'Customer Name', 'Total Value', 'Status', 'Salesman']
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        df = pd.DataFrame(columns=cols)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Combined Quotations', index=False)
        _style_combined_excel_worksheet(writer.sheets['Combined Quotations'])
    output.seek(0)
    filename = 'alabama_combined_quotations_%s.xlsx' % timezone.now().strftime('%Y%m%d_%H%M')
    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = 'attachment; filename="%s"' % filename
    return response


@login_required
@require_GET
def export_combined_quotations_items_excel(request):
    """Combined Alabama SAP + in-app line items; respects combined filters."""
    import pandas as pd
    from io import BytesIO
    from django.utils import timezone
    from so.models import QuotationItem

    source = _combined_list_export_source(request)
    rows = []
    if source in ('all', 'sap'):
        sap_base = alabama_sap_quotations_filtered_qs(request)
        sap_items = (
            AlabamaSAPQuotationItem.objects.filter(quotation__in=sap_base)
            .select_related('quotation')
            .order_by('quotation_id', 'id')
        )
        for li in sap_items.iterator(chunk_size=1000):
            q = li.quotation
            d = q.posting_date
            price = float(li.price or 0)
            qty = float(li.quantity or 0)
            line_total = float(li.row_total) if li.row_total is not None else qty * price
            rows.append({
                'Source': 'SAP',
                'Quotation Number': q.q_number,
                'Date': d.strftime('%Y-%m-%d') if d else '',
                'Customer Code': q.customer_code or '',
                'Customer Name': q.customer_name or '',
                'Salesman': q.salesman_name or '',
                'Item No': li.item_no or '',
                'Item Name': li.description or '',
                'Quantity': qty,
                'Price': price,
                'Total Value': line_total,
            })
    if source in ('all', 'app'):
        app_base = alabama_inapp_quotations_filtered_qs(request)
        app_items = (
            QuotationItem.objects.filter(quotation__in=app_base)
            .select_related('quotation', 'quotation__customer', 'quotation__salesman', 'item')
            .order_by('quotation_id', 'id')
        )
        for li in app_items.iterator(chunk_size=1000):
            qo = li.quotation
            cust = qo.customer
            d = qo.quotation_date
            name = qo.customer_display_name or (cust.customer_name if cust else '')
            item_no = li.item.item_code if li.item_id else ''
            item_name = li.item.item_description if li.item_id else ''
            price = float(li.price or 0)
            qty = float(li.quantity or 0)
            line_total = float(li.line_total or (qty * price))
            rows.append({
                'Source': 'App',
                'Quotation Number': qo.quotation_number or '',
                'Date': d.strftime('%Y-%m-%d') if d else '',
                'Customer Code': cust.customer_code if cust else '',
                'Customer Name': name,
                'Salesman': qo.salesman.salesman_name if qo.salesman_id else '',
                'Item No': item_no,
                'Item Name': item_name,
                'Quantity': qty,
                'Price': price,
                'Total Value': line_total,
            })
    rows.sort(
        key=lambda r: (
            _combined_sort_date(r['Date']),
            r.get('Quotation Number') or '',
            r.get('Source') or '',
            r.get('Item No') or '',
        ),
        reverse=True,
    )
    cols = ['Source', 'Quotation Number', 'Date', 'Customer Code', 'Customer Name', 'Salesman', 'Item No', 'Item Name', 'Quantity', 'Price', 'Total Value']
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        df = pd.DataFrame(columns=cols)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Combined Lines', index=False)
        _style_combined_excel_worksheet(writer.sheets['Combined Lines'])
    output.seek(0)
    filename = 'alabama_combined_quotation_lines_%s.xlsx' % timezone.now().strftime('%Y%m%d_%H%M')
    response = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = 'attachment; filename="%s"' % filename
    return response
