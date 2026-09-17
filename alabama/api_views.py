"""
Alabama JSON APIs -- kept separate from the Junaid (so app) APIs.

Data source is AlabamaSalesLine (Excel-uploaded sales summary), not the SAP
AR Invoice / Credit Memo tables the Junaid equivalent
(so.purchase_stock_requirement_views.api_item_analysis_totals) reads.
"""
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from .models import AlabamaSalesLine
from .views import alabama_salesman_scope_q


def _num(value):
    value = float(value or 0)
    return int(value) if value == int(value) else value


@csrf_exempt
@require_GET
def api_item_analysis_totals(request):
    """
    API endpoint: Alabama item_code with total_qty, ho_qty, others_qty, total_2025, total_2026.
    Same response shape as the Junaid /api/item-analysis-totals/, so a caller can use either.

    Differences from the Junaid API:
    - Credit Memo lines are stored with NEGATIVE quantities in AlabamaSalesLine, so a plain
      SUM over all lines is already net (invoice - credit memo); subtracting again would
      double-count returns.
    - Alabama data has no store split, so ho_qty and others_qty are always 0.

    Optional filter: firm (GET param, repeatable, e.g. ?firm=A&firm=B).
    Response: { "results": [ { "item_code": "...", "total_qty": 10, "ho_qty": 0, "others_qty": 0,
                               "total_2025": 8, "total_2026": 2 }, ... ] }
    """
    firm_list = list(dict.fromkeys(f.strip() for f in request.GET.getlist('firm') if f and f.strip()))

    # alabama_salesman_scope_q errors for an anonymous user, so only scope logged-in callers.
    scope_q = alabama_salesman_scope_q(request.user) if request.user.is_authenticated else Q()
    lines = (AlabamaSalesLine.objects.filter(scope_q)
             .exclude(item__item_code__isnull=True)
             .exclude(item__item_code=''))
    if firm_list:
        lines = lines.filter(item__item_firm__in=firm_list)

    zero = Value(0, output_field=DecimalField())
    rows = (lines.values('item__item_code')
            .annotate(
                total_qty=Coalesce(Sum('quantity'), zero),
                total_2025=Coalesce(Sum('quantity', filter=Q(posting_date__year=2025)), zero),
                total_2026=Coalesce(Sum('quantity', filter=Q(posting_date__year=2026)), zero),
            )
            .order_by('item__item_code'))

    results = [
        {
            'item_code': row['item__item_code'],
            'total_qty': _num(row['total_qty']),
            'total_2025': _num(row['total_2025']),
            'total_2026': _num(row['total_2026']),
        }
        for row in rows
    ]
    return JsonResponse({'results': results})
