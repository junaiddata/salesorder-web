"""Which item catalog a quotation / order screen uses, and how an item is referenced in the forms.

Junaid's items are so.Items; Alabama's own item master is alabama.AlabamaItem. An id from one can equal an id from the
other, so the forms post an Alabama item as 'a:<id>' and a Junaid item as the bare id (which is also exactly what every
existing page, saved draft and email-agent prefill already posts -- a bare id always means a so.Items row).

New Alabama quotations / orders use the Alabama catalog only while settings.ALABAMA_OWN_CATALOG is on AND the screen
says it is working on the ALABAMA division AND the logged-in user may work on it. Everything else (Junaid users, the
direct sales-order create page, every existing document) keeps using so.Items exactly as before."""
from django.conf import settings

ALABAMA_PREFIX = 'a:'


def own_catalog_enabled():
    return bool(getattr(settings, 'ALABAMA_OWN_CATALOG', False))


def parse_item_ref(raw):
    """'a:12' -> ('alabama', 12); '12' -> ('junaid', 12); '' / junk -> (None, None)."""
    raw = str(raw if raw is not None else '').strip()
    if not raw:
        return None, None
    kind = 'junaid'
    if raw.lower().startswith(ALABAMA_PREFIX):
        kind, raw = 'alabama', raw[len(ALABAMA_PREFIX):]
    try:
        return kind, int(raw)
    except (TypeError, ValueError):
        return None, None


def use_alabama_catalog(request, division=None):
    """True when this request must be served from the Alabama catalog.

    `division` is the division of the document being worked on when the server already knows it (editing an existing
    document); otherwise it is read from the `division` the page sends. A user who may not work on Alabama documents
    (e.g. a Junaid salesman) gets False whatever the page sends."""
    if not own_catalog_enabled():
        return False
    requested = division if division is not None else (request.GET.get('division') or request.POST.get('division') or '')
    if str(requested).upper() != 'ALABAMA':
        return False
    from .views_quotation import _quotation_creation_division
    return _quotation_creation_division(request, 'ALABAMA') == 'ALABAMA'


def resolve_items(refs):
    """refs: iterable of raw form values. Returns {raw_value: ('junaid'|'alabama', obj)} for every ref that exists.
    One query per catalog (no N+1)."""
    from alabama.models import AlabamaItem
    from .models import Items

    junaid_ids, alabama_ids, parsed = set(), set(), {}
    for raw in refs:
        kind, pk = parse_item_ref(raw)
        if kind is None:
            continue
        parsed[raw] = (kind, pk)
        (alabama_ids if kind == 'alabama' else junaid_ids).add(pk)
    j = {o.pk: o for o in Items.objects.filter(pk__in=junaid_ids)} if junaid_ids else {}
    a = {o.pk: o for o in AlabamaItem.objects.filter(pk__in=alabama_ids)} if alabama_ids else {}
    out = {}
    for raw, (kind, pk) in parsed.items():
        obj = (a if kind == 'alabama' else j).get(pk)
        if obj is not None:
            out[raw] = (kind, obj)
    return out


def line_kwargs(kind, obj):
    """Model kwargs that attach `obj` to a QuotationItem / OrderItem / CustomerPrice."""
    return {'alabama_item': obj} if kind == 'alabama' else {'item': obj}


def item_option_ref(kind, obj):
    return f'{ALABAMA_PREFIX}{obj.pk}' if kind == 'alabama' else str(obj.pk)
