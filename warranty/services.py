import re

from django.utils import timezone

from .models import WarrantyLetter, WarrantyLetterSettings


def suggest_ref_no(company):
    """Suggest the next '<prefix>/<year>/<seq>' Ref No for a company.

    Scans existing ref_no values for the current year rather than keeping a
    stored counter, since ref_no is not uniqueness-enforced (the user can
    freely edit/reuse/backdate it).
    """
    settings_row = WarrantyLetterSettings.get_instance(company)
    prefix = settings_row.ref_no_prefix or ('JN/WC' if company == 'junaid' else 'AL/WC')
    year = timezone.now().year

    pattern = re.compile(rf'^{re.escape(prefix)}/{year}/(\d+)$')
    max_seq = 0
    qs = WarrantyLetter.objects.filter(
        company=company, ref_no__startswith=f'{prefix}/{year}/'
    ).values_list('ref_no', flat=True)
    for ref in qs:
        m = pattern.match(ref)
        if m:
            max_seq = max(max_seq, int(m.group(1)))

    return f'{prefix}/{year}/{max_seq + 1:04d}'
