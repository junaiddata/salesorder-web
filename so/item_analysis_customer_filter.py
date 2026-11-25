"""Customer filter for the Item Analysis page (saparinvoices/item-analysis/) and its PDF / Excel exports.

GET parameter: `customer` (repeatable), holding SAP customer codes. Always applied on top of querysets that are already
salesman-scoped, so it can only narrow what a user sees.
"""
from .models import SAPARCreditMemo, SAPARInvoice


def selected_customer_codes(request):
    return [c.strip() for c in request.GET.getlist('customer') if c.strip()]


def apply_customer_filter(request, invoice_qs, creditmemo_qs):
    codes = selected_customer_codes(request)
    if codes:
        invoice_qs = invoice_qs.filter(customer_code__in=codes)
        creditmemo_qs = creditmemo_qs.filter(customer_code__in=codes)
    return invoice_qs, creditmemo_qs


def customer_choices(scope_q):
    """[(code, name)] for customers on invoices / credit memos inside the user's salesman scope, sorted by name.
    One entry per code (a shared code such as a cash customer shows one of its names)."""
    names = {}
    for model in (SAPARInvoice, SAPARCreditMemo):
        rows = (model.objects.filter(scope_q)
                .exclude(customer_code__isnull=True).exclude(customer_code='')
                .order_by().values_list('customer_code', 'customer_name').distinct())
        for code, name in rows:
            if code not in names:
                names[code] = (name or '').strip() or code
    return sorted(names.items(), key=lambda x: (x[1].casefold(), x[0]))


def customer_labels(codes, scope_q):
    """Readable 'Name (code)' labels for the selected codes. Names are looked up inside the user's scope only."""
    if not codes:
        return []
    names = {}
    for model in (SAPARInvoice, SAPARCreditMemo):
        rows = (model.objects.filter(scope_q, customer_code__in=codes)
                .order_by().values_list('customer_code', 'customer_name').distinct())
        for code, name in rows:
            names.setdefault(code, (name or '').strip())
    return [f'{names[c]} ({c})' if names.get(c) else c for c in codes]
