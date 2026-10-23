"""Emailing a quotation PDF from the quotation mailbox (QUOTATION_EMAIL_* in settings / .env).

Everything here uses its own connection and credentials, so the EMAIL_* account used by the older quotation and
submittal modules is never touched.
"""
import imaplib
import logging
import re
import time

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage, get_connection
from django.core.validators import validate_email

log = logging.getLogger('quotation')

FROM_NAME = 'Junaid Galleria'


def is_configured():
    return bool(settings.QUOTATION_EMAIL_HOST and settings.QUOTATION_EMAIL_USER and settings.QUOTATION_EMAIL_PASSWORD)


def parse_addresses(text):
    """Split 'a@x.com, b@y.com; c@z.com' into (valid, invalid) lists (duplicates removed, order kept)."""
    valid, invalid, seen = [], [], set()
    for part in re.split(r'[,;\s]+', text or ''):
        part = part.strip()
        if not part or part.lower() in seen:
            continue
        seen.add(part.lower())
        try:
            validate_email(part)
            valid.append(part)
        except ValidationError:
            invalid.append(part)
    return valid, invalid


def _save_copy_to_sent(message, sent_at):
    """File a copy in the mailbox's IMAP Sent folder (SMTP alone leaves nothing there). Best effort: never raises,
    because the customer already has the mail by then."""
    from emailagent.sent_mail import _discover_sent_folder, _quote_folder   # reuse the folder discovery, not the other mailbox's login
    host, port = settings.QUOTATION_IMAP_HOST, settings.QUOTATION_IMAP_PORT
    if not (host and port):
        return False
    conn = None
    try:
        raw = message.message().as_bytes()
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(settings.QUOTATION_EMAIL_USER, settings.QUOTATION_EMAIL_PASSWORD)
        status, _ = conn.append(_quote_folder(_discover_sent_folder(conn)), r'(\Seen)', imaplib.Time2Internaldate(sent_at), raw)
        return status == 'OK'
    except Exception:
        log.exception('Could not file the quotation email in the Sent folder')
        return False
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass


def send_quotation(*, to, cc, subject, body, pdf_bytes, filename):
    """Send the PDF. Returns (ok, error_text, saved_to_sent). Never raises."""
    sender = settings.QUOTATION_EMAIL_USER
    try:
        connection = get_connection(
            host=settings.QUOTATION_EMAIL_HOST, port=settings.QUOTATION_EMAIL_PORT,
            username=settings.QUOTATION_EMAIL_USER, password=settings.QUOTATION_EMAIL_PASSWORD,
            use_tls=settings.QUOTATION_EMAIL_USE_TLS, timeout=30)
        message = EmailMessage(subject=subject, body=body, from_email=f'{FROM_NAME} <{sender}>',
                               to=to, cc=cc or None, reply_to=[sender], connection=connection)
        message.attach(filename, pdf_bytes, 'application/pdf')
        sent_at = time.time()
        message.send(fail_silently=False)
    except Exception as e:
        log.exception('Quotation email failed')
        return False, f'{type(e).__name__}: {e}'[:500], False
    return True, '', _save_copy_to_sent(message, sent_at)
