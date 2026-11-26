"""
Proposed 2027 budgets of the Item Monthly Sales page: one value per item on each tab.

  Sales Amount tab               -> amount      (whole number)
  Average Unit Selling Price tab -> unit_price  (up to 2 decimals)
  Sold Qty tab                   -> qty         (whole number)

The figures are company-wide. Everyone sees them, only Role = Admin may change them. All value checks are in
parse_budget_value and all writing is in set_budget, so a later file upload can follow the same rules.
"""
import re
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import ItemBudget, Items, SAPARCreditMemoItem, SAPARInvoiceItem

BUDGET_YEAR = 2027

LABELS = {
    'amount': f'Proposed Budget Sales Amount {BUDGET_YEAR}',
    'unit_price': f'Proposed Budget Average Unit Selling Price {BUDGET_YEAR}',
    'qty': f'Proposed Budget Qty {BUDGET_YEAR}',
}
VIEW_FIELD = {'amount': 'amount', 'price': 'unit_price', 'qty': 'qty'}   # tab -> ItemBudget field

_MAX = {'qty': Decimal('999999999'), 'amount': Decimal('999999999999'), 'unit_price': Decimal('999999999.99')}
# digits, optionally grouped in thousands (1,500) - "1,5" is refused, never read as 15
_INT_PART = r'(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)'
_WHOLE = re.compile(_INT_PART)
_TWO_DECIMALS = re.compile(_INT_PART + r'(?:\.[0-9]{1,2})?')


def can_edit_budget(user):
    """Only users whose Role is Admin."""
    try:
        return user.role.role == 'Admin'
    except Exception:   # no Role row
        return False


def parse_budget_value(field, raw):
    """Text -> value for that budget field; '' -> None (remove). Raises ValueError with a message for the user."""
    raw = raw.strip()
    if not raw:
        return None
    price = field == 'unit_price'
    message = 'Enter a number (0 or more, up to 2 decimals).' if price else 'Enter a whole number (0 or more).'
    if len(raw) > 20 or not (_TWO_DECIMALS if price else _WHOLE).fullmatch(raw):
        raise ValueError(message)
    value = Decimal(raw.replace(',', ''))
    if value > _MAX[field]:
        raise ValueError('The number is too large.')
    return value.quantize(Decimal('0.01')) if price else int(value)


def budgets_for(field):
    """{item_code: value} of one budget field, for the items that have it."""
    return dict(ItemBudget.objects.filter(year=BUDGET_YEAR, **{f'{field}__isnull': False})
                .values_list('item_code', field))


def budget_text(value):
    """1,500  /  12.50  - for read-only display."""
    return '' if value is None else f'{value:,}'


def add_budget_cells(rows, field):
    """Screen texts of each row's r['budget']."""
    for r in rows:
        r['budget_text'] = budget_text(r['budget'])
        r['budget_input'] = '' if r['budget'] is None else str(r['budget'])


@transaction.atomic
def set_budget(code, field, value, user):
    """Write ONE budget field of an item; the other two are never touched. None removes that value."""
    if value is not None:
        ItemBudget.objects.update_or_create(
            item_code=code, year=BUDGET_YEAR, defaults={field: value, 'updated_by': user})
        return
    entry = ItemBudget.objects.filter(item_code=code, year=BUDGET_YEAR)
    entry.update(**{field: None}, updated_by=user, updated_at=timezone.now())
    entry.filter(qty__isnull=True, amount__isnull=True, unit_price__isnull=True).delete()   # nothing left


def _item_exists(code):
    return (Items.objects.filter(item_code=code).exists()
            or SAPARInvoiceItem.objects.filter(item_code=code).exists()
            or SAPARCreditMemoItem.objects.filter(item_code=code).exists())


@login_required
@require_POST
def save_item_budget(request):
    """POST item + exactly one of qty / amount / unit_price. An empty value removes only that one."""
    if not can_edit_budget(request.user):
        return JsonResponse({'ok': False, 'error': 'Only an admin can change the budget.'}, status=403)

    code = request.POST.get('item', '').strip()
    fields = [name for name in LABELS if name in request.POST]
    if not code or len(code) > 100 or len(fields) != 1:
        return JsonResponse({'ok': False, 'error': 'Bad request.'}, status=400)
    field = fields[0]
    try:
        value = parse_budget_value(field, request.POST[field])
    except ValueError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

    if value is not None and not _item_exists(code):
        return JsonResponse({'ok': False, 'error': 'Unknown item.'}, status=400)
    set_budget(code, field, value, request.user)
    return JsonResponse({'ok': True, field: str(value) if isinstance(value, Decimal) else value})
