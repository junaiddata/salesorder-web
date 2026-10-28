"""Read-only look-ups in the Salesman list (so.Salesman) for the Sales Person / Email / Contact Number boxes.

Nothing here writes to that table, and any failure just means "no match" so the quotation form keeps working.
"""
import logging
import re

log = logging.getLogger('quotation')


def _clean(value):
    return ' '.join(str(value or '').split())


def _row(sm):
    return {'name': _clean(sm.salesman_name), 'phone': _clean(sm.phone), 'email': _clean(sm.email)}


def salesman_for_user(user):
    """The Salesman row whose name equals the user's full name or username (any capitals / spacing), else None."""
    try:
        from so.models import Salesman
        names = [user.get_full_name(), user.get_username(), re.sub(r'[._-]+', ' ', user.get_username())]
        for name in dict.fromkeys(_clean(n) for n in names):
            if name:
                sm = Salesman.objects.filter(salesman_name__iexact=name).order_by('id').first()
                if sm:
                    return _row(sm)
    except Exception:
        log.exception('Salesman look-up for the logged-in user failed')
    return None


def search_salesmen(text, limit=15):
    """Salesmen whose name contains every word typed (any case), best matches first."""
    tokens = [t for t in re.split(r'[\s\-/.]+', (text or '').strip()) if t]
    if not tokens:
        return []
    try:
        from so.models import Salesman
        qs = Salesman.objects.all()
        for t in tokens:
            qs = qs.filter(salesman_name__icontains=t)
        first = ' '.join(tokens).lower()
        rows = [_row(s) for s in qs.order_by('salesman_name')[:100]]
        rows.sort(key=lambda r: (not r['name'].lower().startswith(first), r['name'].lower()))
        return rows[:limit]
    except Exception:
        log.exception('Salesman search failed')
        return []
