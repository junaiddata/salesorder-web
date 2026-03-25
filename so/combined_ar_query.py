"""
Shared query/filter logic for combined AR Invoice + Credit Memo lists.
Used by combined_sales_invoices_list and accounts_recording_list.
"""

from datetime import datetime, date

from django.db.models import Q, Sum, Value, DecimalField
from django.db.models.functions import Coalesce

from .models import SAPARInvoice, SAPARCreditMemo

# Inclusive floor for posting_date on views that should only list recent AR (e.g. Accounts Recording).
ACCOUNTS_RECORDING_POSTING_DATE_FLOOR = date(2026, 1, 1)


def _parse_combined_date(s):
    if not s:
        return None
    try:
        if len(s) == 7:
            return datetime.strptime(s + "-01", "%Y-%m-%d").date()
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def get_combined_ar_request_params(request):
    """Parse GET parameters shared by combined AR views."""
    return {
        "q": request.GET.get("q", "").strip(),
        "salesmen_filter": request.GET.getlist("salesman"),
        "cancel_status_filter": request.GET.get("cancel_status", "").strip(),
        "store_filter": request.GET.get("store", "").strip(),
        "document_type_filter": request.GET.get("document_type", "").strip(),
        "start": request.GET.get("start", "").strip(),
        "end": request.GET.get("end", "").strip(),
        "total_range": request.GET.get("total", "").strip(),
    }


def apply_combined_ar_filters(
    qs,
    *,
    is_invoice=True,
    q="",
    salesmen_filter=None,
    cancel_status_filter="",
    store_filter="",
    total_range="",
    start="",
    end="",
    posting_date_start_floor=None,
):
    if salesmen_filter is None:
        salesmen_filter = []
    if store_filter:
        qs = qs.filter(store=store_filter)
    if salesmen_filter:
        clean_salesmen = [s for s in salesmen_filter if s.strip()]
        if clean_salesmen:
            qs = qs.filter(salesman_name__in=clean_salesmen)

    if cancel_status_filter:
        if cancel_status_filter == "csNo":
            qs = qs.filter(cancel_status="csNo")
        elif cancel_status_filter == "csYes":
            qs = qs.filter(cancel_status="csYes")
        elif cancel_status_filter == "csCancellation":
            qs = qs.filter(cancel_status="csCancellation")
        elif cancel_status_filter == "All":
            pass
        else:
            qs = qs.filter(cancel_status=cancel_status_filter)

    if total_range:
        if total_range == "0-5000":
            qs = qs.filter(doc_total__gte=0, doc_total__lte=5000)
        elif total_range == "5001-10000":
            qs = qs.filter(doc_total__gte=5001, doc_total__lte=10000)
        elif total_range == "10001-25000":
            qs = qs.filter(doc_total__gte=10001, doc_total__lte=25000)
        elif total_range == "25001-50000":
            qs = qs.filter(doc_total__gte=25001, doc_total__lte=50000)
        elif total_range == "50001-100000":
            qs = qs.filter(doc_total__gte=50001, doc_total__lte=100000)
        elif total_range == "100000+":
            qs = qs.filter(doc_total__gt=100000)

    if q:
        if is_invoice:
            if q.isdigit():
                qs = qs.filter(invoice_number__istartswith=q)
            elif len(q) < 3:
                qs = qs.filter(
                    Q(customer_name__istartswith=q)
                    | Q(salesman_name__istartswith=q)
                    | Q(bp_reference_no__istartswith=q)
                )
            else:
                qs = qs.filter(
                    Q(invoice_number__icontains=q)
                    | Q(customer_name__icontains=q)
                    | Q(salesman_name__icontains=q)
                    | Q(bp_reference_no__icontains=q)
                    | Q(customer_code__icontains=q)
                )
        else:
            if q.isdigit():
                qs = qs.filter(credit_memo_number__istartswith=q)
            elif len(q) < 3:
                qs = qs.filter(
                    Q(customer_name__istartswith=q)
                    | Q(salesman_name__istartswith=q)
                    | Q(bp_reference_no__istartswith=q)
                )
            else:
                qs = qs.filter(
                    Q(credit_memo_number__icontains=q)
                    | Q(customer_name__icontains=q)
                    | Q(salesman_name__icontains=q)
                    | Q(bp_reference_no__icontains=q)
                    | Q(customer_code__icontains=q)
                )

    start_date = _parse_combined_date(start)
    end_date = _parse_combined_date(end)
    if posting_date_start_floor is not None:
        if start_date is None:
            start_date = posting_date_start_floor
        else:
            start_date = max(start_date, posting_date_start_floor)
    if start_date:
        qs = qs.filter(posting_date__gte=start_date)
    if end_date:
        qs = qs.filter(posting_date__lte=end_date)

    return qs


def get_combined_ar_filtered_querysets(
    request,
    user,
    scope_q_fn,
    default_store_when_unspecified=None,
    posting_date_start_floor=None,
):
    """
    Return (invoice_qs, creditmemo_qs, params_dict) with filters applied.
    scope_q_fn: callable user -> Q for salesman scope (e.g. salesman_scope_q_salesorder).
    If default_store_when_unspecified is set (e.g. "HO"), it applies only when the request
    has no ``store`` query parameter (first visit). Submitting store= (All Stores) still clears it.
    If posting_date_start_floor is set, posting_date is never before that date; ``p["start"]`` is
    updated so filter forms show the effective start date.
    """
    p = dict(get_combined_ar_request_params(request))
    if default_store_when_unspecified is not None and "store" not in request.GET:
        p["store_filter"] = default_store_when_unspecified
    if posting_date_start_floor is not None:
        parsed_start = _parse_combined_date(p["start"])
        effective_start = (
            max(parsed_start, posting_date_start_floor)
            if parsed_start
            else posting_date_start_floor
        )
        p["start"] = effective_start.isoformat()
    invoice_qs = SAPARInvoice.objects.all()
    creditmemo_qs = SAPARCreditMemo.objects.all()

    if not (user.is_superuser or user.is_staff):
        sq = scope_q_fn(user)
        invoice_qs = invoice_qs.filter(sq)
        creditmemo_qs = creditmemo_qs.filter(sq)

    invoice_qs = apply_combined_ar_filters(
        invoice_qs,
        is_invoice=True,
        q=p["q"],
        salesmen_filter=p["salesmen_filter"],
        cancel_status_filter=p["cancel_status_filter"],
        store_filter=p["store_filter"],
        total_range=p["total_range"],
        start=p["start"],
        end=p["end"],
        posting_date_start_floor=posting_date_start_floor,
    )
    creditmemo_qs = apply_combined_ar_filters(
        creditmemo_qs,
        is_invoice=False,
        q=p["q"],
        salesmen_filter=p["salesmen_filter"],
        cancel_status_filter=p["cancel_status_filter"],
        store_filter=p["store_filter"],
        total_range=p["total_range"],
        start=p["start"],
        end=p["end"],
        posting_date_start_floor=posting_date_start_floor,
    )

    if p["document_type_filter"] == "Invoice":
        creditmemo_qs = creditmemo_qs.none()
    elif p["document_type_filter"] == "Credit Memo":
        invoice_qs = invoice_qs.none()

    return invoice_qs, creditmemo_qs, p


def combined_ar_totals(invoice_qs, creditmemo_qs):
    invoice_totals = invoice_qs.aggregate(
        total_without_vat=Coalesce(Sum("doc_total_without_vat"), Value(0, output_field=DecimalField())),
        total_gross_profit=Coalesce(Sum("total_gross_profit"), Value(0, output_field=DecimalField())),
    )
    creditmemo_totals = creditmemo_qs.aggregate(
        total_without_vat=Coalesce(Sum("doc_total_without_vat"), Value(0, output_field=DecimalField())),
        total_gross_profit=Coalesce(Sum("total_gross_profit"), Value(0, output_field=DecimalField())),
    )
    return (
        invoice_totals["total_without_vat"] + creditmemo_totals["total_without_vat"],
        invoice_totals["total_gross_profit"] + creditmemo_totals["total_gross_profit"],
    )


def combined_ar_summary_metrics(
    user,
    scope_q_fn,
    store_filter,
    salesmen_filter,
    posting_date_floor=None,
):
    """Today / month / year sales and GP (without VAT), honoring store + salesman filters on base qs."""
    today = date.today()
    current_year = today.year
    current_month = today.month

    today_invoices = SAPARInvoice.objects.all()
    today_creditmemos = SAPARCreditMemo.objects.all()
    if not (user.is_superuser or user.is_staff):
        sq = scope_q_fn(user)
        today_invoices = today_invoices.filter(sq)
        today_creditmemos = today_creditmemos.filter(sq)
    today_invoices = today_invoices.filter(posting_date=today)
    today_creditmemos = today_creditmemos.filter(posting_date=today)
    if posting_date_floor is not None:
        today_invoices = today_invoices.filter(posting_date__gte=posting_date_floor)
        today_creditmemos = today_creditmemos.filter(posting_date__gte=posting_date_floor)
    if store_filter:
        today_invoices = today_invoices.filter(store=store_filter)
        today_creditmemos = today_creditmemos.filter(store=store_filter)
    if salesmen_filter:
        clean_salesmen = [s for s in salesmen_filter if s.strip()]
        if clean_salesmen:
            today_invoices = today_invoices.filter(salesman_name__in=clean_salesmen)
            today_creditmemos = today_creditmemos.filter(salesman_name__in=clean_salesmen)

    today_sales_agg = today_invoices.aggregate(
        total=Coalesce(Sum("doc_total_without_vat"), Value(0, output_field=DecimalField()))
    )
    today_sales_cm_agg = today_creditmemos.aggregate(
        total=Coalesce(Sum("doc_total_without_vat"), Value(0, output_field=DecimalField()))
    )
    today_sales = today_sales_agg["total"] + today_sales_cm_agg["total"]

    today_gp_agg = today_invoices.aggregate(
        total=Coalesce(Sum("total_gross_profit"), Value(0, output_field=DecimalField()))
    )
    today_gp_cm_agg = today_creditmemos.aggregate(
        total=Coalesce(Sum("total_gross_profit"), Value(0, output_field=DecimalField()))
    )
    today_gp = today_gp_agg["total"] + today_gp_cm_agg["total"]

    month_invoices = SAPARInvoice.objects.all()
    month_creditmemos = SAPARCreditMemo.objects.all()
    if not (user.is_superuser or user.is_staff):
        sq = scope_q_fn(user)
        month_invoices = month_invoices.filter(sq)
        month_creditmemos = month_creditmemos.filter(sq)
    month_invoices = month_invoices.filter(posting_date__year=current_year, posting_date__month=current_month)
    month_creditmemos = month_creditmemos.filter(posting_date__year=current_year, posting_date__month=current_month)
    if posting_date_floor is not None:
        month_invoices = month_invoices.filter(posting_date__gte=posting_date_floor)
        month_creditmemos = month_creditmemos.filter(posting_date__gte=posting_date_floor)
    if store_filter:
        month_invoices = month_invoices.filter(store=store_filter)
        month_creditmemos = month_creditmemos.filter(store=store_filter)
    if salesmen_filter:
        clean_salesmen = [s for s in salesmen_filter if s.strip()]
        if clean_salesmen:
            month_invoices = month_invoices.filter(salesman_name__in=clean_salesmen)
            month_creditmemos = month_creditmemos.filter(salesman_name__in=clean_salesmen)

    month_sales_agg = month_invoices.aggregate(
        total=Coalesce(Sum("doc_total_without_vat"), Value(0, output_field=DecimalField()))
    )
    month_sales_cm_agg = month_creditmemos.aggregate(
        total=Coalesce(Sum("doc_total_without_vat"), Value(0, output_field=DecimalField()))
    )
    month_sales = month_sales_agg["total"] + month_sales_cm_agg["total"]

    month_gp_agg = month_invoices.aggregate(
        total=Coalesce(Sum("total_gross_profit"), Value(0, output_field=DecimalField()))
    )
    month_gp_cm_agg = month_creditmemos.aggregate(
        total=Coalesce(Sum("total_gross_profit"), Value(0, output_field=DecimalField()))
    )
    month_gp = month_gp_agg["total"] + month_gp_cm_agg["total"]

    year_invoices = SAPARInvoice.objects.all()
    year_creditmemos = SAPARCreditMemo.objects.all()
    if not (user.is_superuser or user.is_staff):
        sq = scope_q_fn(user)
        year_invoices = year_invoices.filter(sq)
        year_creditmemos = year_creditmemos.filter(sq)
    year_invoices = year_invoices.filter(posting_date__year=current_year)
    year_creditmemos = year_creditmemos.filter(posting_date__year=current_year)
    if posting_date_floor is not None:
        year_invoices = year_invoices.filter(posting_date__gte=posting_date_floor)
        year_creditmemos = year_creditmemos.filter(posting_date__gte=posting_date_floor)
    if store_filter:
        year_invoices = year_invoices.filter(store=store_filter)
        year_creditmemos = year_creditmemos.filter(store=store_filter)
    if salesmen_filter:
        clean_salesmen = [s for s in salesmen_filter if s.strip()]
        if clean_salesmen:
            year_invoices = year_invoices.filter(salesman_name__in=clean_salesmen)
            year_creditmemos = year_creditmemos.filter(salesman_name__in=clean_salesmen)

    year_sales_agg = year_invoices.aggregate(
        total=Coalesce(Sum("doc_total_without_vat"), Value(0, output_field=DecimalField()))
    )
    year_sales_cm_agg = year_creditmemos.aggregate(
        total=Coalesce(Sum("doc_total_without_vat"), Value(0, output_field=DecimalField()))
    )
    year_sales = year_sales_agg["total"] + year_sales_cm_agg["total"]

    year_gp_agg = year_invoices.aggregate(
        total=Coalesce(Sum("total_gross_profit"), Value(0, output_field=DecimalField()))
    )
    year_gp_cm_agg = year_creditmemos.aggregate(
        total=Coalesce(Sum("total_gross_profit"), Value(0, output_field=DecimalField()))
    )
    year_gp = year_gp_agg["total"] + year_gp_cm_agg["total"]

    return {
        "today_sales": today_sales,
        "today_gp": today_gp,
        "month_sales": month_sales,
        "month_gp": month_gp,
        "year_sales": year_sales,
        "year_gp": year_gp,
    }


def combined_ar_salesmen_list(user, scope_q_fn):
    invoice_salesmen = (
        SAPARInvoice.objects.filter(scope_q_fn(user))
        .exclude(salesman_name__isnull=True)
        .exclude(salesman_name="")
        .values_list("salesman_name", flat=True)
        .distinct()
    )
    creditmemo_salesmen = (
        SAPARCreditMemo.objects.filter(scope_q_fn(user))
        .exclude(salesman_name__isnull=True)
        .exclude(salesman_name="")
        .values_list("salesman_name", flat=True)
        .distinct()
    )
    return sorted(set(list(invoice_salesmen) + list(creditmemo_salesmen)))


def combined_ar_ho_salesmen_list(user, scope_q_fn):
    """Distinct salesman names on documents with store=HO (scoped). For Accounts Recording handover dropdown."""
    inv = SAPARInvoice.objects.filter(store="HO").filter(scope_q_fn(user))
    cm = SAPARCreditMemo.objects.filter(store="HO").filter(scope_q_fn(user))
    invoice_salesmen = (
        inv.exclude(salesman_name__isnull=True)
        .exclude(salesman_name="")
        .values_list("salesman_name", flat=True)
        .distinct()
    )
    creditmemo_salesmen = (
        cm.exclude(salesman_name__isnull=True)
        .exclude(salesman_name="")
        .values_list("salesman_name", flat=True)
        .distinct()
    )
    return sorted(set(list(invoice_salesmen) + list(creditmemo_salesmen)))
