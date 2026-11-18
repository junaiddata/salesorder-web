"""Who created an in-app quotation (display only; nothing is stored on the quotation).

Order of lookup:
  1. the 'created' QuotationLog row written when the quotation was made in the web app;
  2. for quotations drafted by the email agent: the person who uploaded the enquiry
     (TrackedEmail.uploaded_by, manual uploads), else 'Email agent' for mailbox-fetched enquiries;
  3. otherwise '' (templates show a dash).
Any failure returns '' so a display problem can never break a page.
"""


def _user_label(user):
    if not user:
        return ''
    return (user.get_full_name() or user.get_username() or '').strip()


def _agent_creator(quotation):
    """Creator for a quotation the email agent drafted, or ''."""
    draft = getattr(quotation, 'quotation_draft', None) or getattr(quotation, 'additional_quotation_draft', None)
    if not draft:
        return ''
    tracked = getattr(draft, 'tracked_email', None)
    if not tracked:
        return ''
    name = _user_label(getattr(tracked, 'uploaded_by', None))
    return name or 'Email agent'


def attach_creator_names(quotations):
    """Set `.creator_name` on each quotation in the iterable (one extra query for the whole batch).
    Returns the same quotations so it can be used inline."""
    quotations = list(quotations)
    if not quotations:
        return quotations
    by_quotation = {}
    try:
        from .models import QuotationLog
        rows = (QuotationLog.objects
                .filter(quotation_id__in=[q.pk for q in quotations], action='created')
                .select_related('user')
                .order_by('id'))
        for row in rows:
            by_quotation.setdefault(row.quotation_id, _user_label(row.user))
    except Exception:
        by_quotation = {}
    for q in quotations:
        name = by_quotation.get(q.pk, '')
        if not name:
            try:
                name = _agent_creator(q)
            except Exception:
                name = ''
        q.creator_name = name
    return quotations
