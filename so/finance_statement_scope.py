"""
Finance Statement access scope: same salesman-name rules as SAP sales orders (SALES_USER_MAP).
Non-admin users only see customers whose assigned Salesman.salesman_name matches their mapping.
"""
from django.db.models import Q
from django.http import Http404

from so.views import SALES_USER_MAP


def finance_statement_user_sees_all_customers(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    role = getattr(getattr(user, "role", None), "role", None)
    if role == "Admin":
        return True
    return False


def finance_statement_customer_scope_q(user) -> Q:
    """
    Restrict Customer rows for finance list/detail/export.
    Returns empty Q (no restriction) for superuser and Admin role.
    """
    if finance_statement_user_sees_all_customers(user):
        return Q()

    uname = (user.username or "").strip().lower()
    names = SALES_USER_MAP.get(uname)
    if names:
        q = Q()
        for n in names:
            q |= Q(salesman__salesman_name__iexact=n)
        return q

    token = uname.replace(".", " ").strip()
    if token:
        return Q(salesman__salesman_name__icontains=token)

    return Q(pk__in=[])


def assert_user_can_access_finance_customer(request, customer) -> None:
    """Raise Http404 if this user may not view/export finance for this customer."""
    from so.models import Customer

    if finance_statement_user_sees_all_customers(request.user):
        return
    if not Customer.objects.filter(pk=customer.pk).filter(finance_statement_customer_scope_q(request.user)).exists():
        raise Http404()
