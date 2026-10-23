"""Email button: send a quotation (always with the company stamp) from the quotation mailbox, and keep a log of every send."""
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import mailer
from .models import Quotation, QuotationEmail, QuotationStamp
from .pdf_builder import build_quotation_pdf
from .views import _public_base_url

log = logging.getLogger('quotation')


def _default_subject(q):
    return f'Quotation {q.qtn_no} - Junaid Galleria' + (f' - {q.project_reference}' if q.project_reference else '')


def _default_body(q):
    lines = ['Dear Sir/Madam,', '',
             f'Please find attached our quotation {q.qtn_no} dated {q.date.strftime("%d %b %Y")}'
             + (f' for {q.project_reference}.' if q.project_reference else '.'), '',
             'Kindly review it and let us know if you need any clarification.', '', 'Best regards,']
    lines += [x for x in (q.sales_person, q.contact_number, 'Junaid Galleria Trading LLC') if x]
    return '\n'.join(lines)


@login_required
def quotation_email(request, pk):
    quotation = get_object_or_404(Quotation, pk=pk)
    errors = []
    values = {'to': quotation.customer_email, 'cc': '', 'subject': _default_subject(quotation),
              'body': _default_body(quotation), 'orientation': 'portrait'}

    if request.method == 'POST':
        values = {k: (request.POST.get(k) or '').strip() for k in ('to', 'cc', 'subject', 'body')}
        values['orientation'] = 'landscape' if request.POST.get('orientation') == 'landscape' else 'portrait'
        if not mailer.is_configured():
            errors.append('Email is not set up yet on the server (QUOTATION_EMAIL_USER / QUOTATION_EMAIL_PASSWORD).')
        to, bad_to = mailer.parse_addresses(values['to'])
        cc, bad_cc = mailer.parse_addresses(values['cc'])
        if not to:
            errors.append('Please enter the customer email address.')
        if bad_to or bad_cc:
            errors.append('Not a valid email address: ' + ', '.join(bad_to + bad_cc))
        if not values['subject']:
            errors.append('Please enter a subject.')
        if not values['body']:
            errors.append('Please enter a message.')

        if not errors:
            quotation.add_stamp = True   # emailed quotations always carry the company stamp (this copy only: not saved)
            pdf = build_quotation_pdf(quotation, _public_base_url(request), values['orientation'], with_stamp=True)
            safe = ''.join(c if c.isalnum() or c in '-_' else '-' for c in quotation.qtn_no)
            ok, error, copied = mailer.send_quotation(
                to=to, cc=cc, subject=values['subject'], body=values['body'], pdf_bytes=pdf,
                filename=f'Quotation_{safe}.pdf')
            QuotationEmail.objects.create(
                quotation=quotation, to_addrs=', '.join(to), cc_addrs=', '.join(cc), subject=values['subject'],
                orientation=values['orientation'], sent_by=request.user, sent_at=timezone.now(),
                success=ok, error=error, saved_to_sent=copied)
            if ok:
                if quotation.customer_email != ', '.join(to):   # remembered for the next time (no other field is touched)
                    Quotation.objects.filter(pk=quotation.pk).update(customer_email=', '.join(to))
                log.info('Quotation emailed: user=%s qtn=%s to=%s', request.user.username, quotation.qtn_no, to)
                messages.success(request, f'Quotation {quotation.qtn_no} sent to {", ".join(to)}.')
                if not copied:
                    messages.warning(request, 'It was sent, but a copy could not be saved to the Sent folder. No need to resend.')
                return redirect('quotation:list')
            errors.append(f'The email could not be sent: {error}')

    return render(request, 'quotation/email.html', {
        'quotation': quotation, 'values': values, 'errors': errors,
        'history': QuotationEmail.objects.filter(quotation=quotation).select_related('sent_by'), 'configured': mailer.is_configured(),
        'has_stamp': bool(QuotationStamp.get_instance().stamp), 'from_addr': settings.QUOTATION_EMAIL_USER,
    })
