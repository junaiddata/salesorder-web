"""
Customer details for the so.Quotation PDF header and the quotation forms.

Each detail comes from the first non-empty source, in this order:
  1. the value typed on the quotation itself (Quotation.customer_address, ...)
  2. the quote-builder customer list (quotation.models.Customer) -- matched by customer code, else by an exact
     (case/spacing-insensitive) unique name
  3. the so.Customer record (filled by the SAP sync)

The quote-builder list is only ever read here, never written. Any failure while looking it up just means "no data from
that list": the caller still gets whatever the other sources have.
"""
import logging

log = logging.getLogger(__name__)

FIELDS = ('contact_person', 'contact_number', 'address', 'trn', 'email')


def _norm(text):
    return ' '.join((text or '').lower().split())


def galleria_customer(customer, extra_names=()):
    """The quote-builder customer matching this so.Customer (by code, then by unique name), or None."""
    try:
        from quotation.models import Customer as ListedCustomer

        code = (getattr(customer, 'customer_code', '') or '').strip().upper()
        if code:
            found = ListedCustomer.objects.filter(code_key=code).first()
            if found:
                return found
        wanted = {_norm(n) for n in (*extra_names, getattr(customer, 'customer_name', '')) if _norm(n)}
        if wanted:
            matches = [pk for pk, name in ListedCustomer.objects.values_list('pk', 'name') if _norm(name) in wanted]
            if len(matches) == 1:
                return ListedCustomer.objects.filter(pk=matches[0]).first()
    except Exception:
        log.exception('Quote-builder customer lookup failed')
    return None


def _clean(value):
    return str(value).strip() if value else ''


def customer_details(customer, quotation=None):
    """{contact_person, contact_number, address, trn, email} for a so.Customer, plus the quotation's own values if given."""
    names = (quotation.customer_display_name,) if quotation is not None and quotation.customer_display_name else ()
    listed = galleria_customer(customer, names) if customer is not None else None
    own = {
        'contact_person': _clean(getattr(quotation, 'customer_contact_person', '')),
        'contact_number': _clean(getattr(quotation, 'customer_contact_number', '')),
        'address': _clean(getattr(quotation, 'customer_address', '')),
        'trn': _clean(getattr(quotation, 'customer_trn', '')),
        'email': _clean(getattr(quotation, 'customer_email', '')),
    } if quotation is not None else {}
    from_list = {
        'contact_person': _clean(listed.contact_person), 'contact_number': _clean(listed.contact_number),
        'address': _clean(listed.address), 'trn': _clean(listed.trn), 'email': _clean(listed.email),
    } if listed else {}
    from_so = {
        'contact_person': '', 'contact_number': _clean(getattr(customer, 'phone_number', '')),
        'address': _clean(getattr(customer, 'address', '')), 'trn': _clean(getattr(customer, 'vat_number', '')),
        'email': '',
    }
    return {f: own.get(f) or from_list.get(f) or from_so[f] for f in FIELDS}
