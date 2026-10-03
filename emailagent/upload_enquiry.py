"""
Manual enquiry upload
=====================
Lets a person upload the enquiry files themselves (Excel / PDF / image BOQ)
instead of waiting for them to arrive by email. The upload is stored as a
TrackedEmail (source='upload') with its files as EmailAttachment rows, then
goes through the SAME pipeline an emailed RFQ does:

    classifier.classify_email   -> EnquiryItem rows
    quotation_agent.draft_quotation -> a real so.Quotation + QuotationDraft

so reviewing, editing, sending and every follow-up behave exactly as for
email-sourced quotations. The only upload-specific steps are the customer /
salesman names typed on the form (applied afterwards) and the duplicate-file
check.
"""
import hashlib
import logging
import threading
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone

from . import classifier, quotation_agent, supervisor
from .models import (
    AgentRun, EmailAttachment, EnquiryItem, QuotationDraft, TrackedEmail,
)

logger = logging.getLogger(__name__)

# Extension -> content type the classifier understands (PDF text/pages, Excel
# sheets, and images it reads visually).
ALLOWED_TYPES = {
    '.pdf': 'application/pdf',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.xls': 'application/vnd.ms-excel',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
    '.gif': 'image/gif',
}
ACCEPT_ATTR = ','.join(sorted(ALLOWED_TYPES))
STALLED_AFTER_MINUTES = 20


def content_type_for(filename):
    name = (filename or '').lower()
    for ext, ctype in ALLOWED_TYPES.items():
        if name.endswith(ext):
            return ctype
    return ''


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _truncated(value, max_length):
    value = value or ''
    return value if len(value) <= max_length else value[:max_length]


# ── duplicate check ─────────────────────────────────────────────────────

def find_duplicates(files_info):
    """files_info: [{'name', 'size', 'hash'}] (hash may be '' when the browser could not
    compute one). A file counts as already received when an attachment with the same
    content hash exists, or -- for files with no stored hash, i.e. everything that came
    by email -- the same file name and size."""
    matches = []
    for info in files_info:
        q = Q(filename=info['name'], size_bytes=info['size'])
        if info.get('hash'):
            q |= Q(file_hash=info['hash'])
        existing = (EmailAttachment.objects.filter(q)
                    .select_related('tracked_email', 'tracked_email__quotation_draft__quotation')
                    .order_by('-id').first())
        if not existing:
            continue
        te = existing.tracked_email
        draft = getattr(te, 'quotation_draft', None)
        quotation = getattr(draft, 'quotation', None) if draft else None
        matches.append({
            'filename': info['name'],
            'received_at': timezone.localtime(te.received_at).strftime('%d %b %Y %H:%M'),
            'source': te.get_source_display(),
            'sender': te.sender_name or te.sender,
            'subject': te.subject,
            'quotation_number': getattr(quotation, 'quotation_number', '') if quotation else '',
            'email_id': te.pk,
        })
    return matches


# ── create the upload ───────────────────────────────────────────────────

def create_upload(user, files, subject='', note='', customer=None, salesman=None, customer_display_name=''):
    """Stores the files and returns the new (still 'pending') TrackedEmail. `customer` /
    `salesman` are the objects picked in the form's dropdowns (both optional);
    `customer_display_name` is the optional name to print on the quotation (walk-in/cash)."""
    customer_display_name = (customer_display_name or '').strip()
    customer_label = customer.customer_name if customer else customer_display_name
    salesman_label = salesman.salesman_name if salesman else ''
    first_name = files[0].name if files else 'enquiry'
    body_lines = [f'[Manual upload by {user.get_username()}]']
    if customer_label:
        body_lines.append(f'Customer: {customer_label}')
    if salesman_label:
        body_lines.append(f'Salesman: {salesman_label}')
    if note:
        body_lines.append('')
        body_lines.append(note.strip())

    with transaction.atomic():
        tracked_email = TrackedEmail.objects.create(
            gmail_message_id=f'upload-{uuid.uuid4().hex}',
            source=TrackedEmail.SOURCE_UPLOAD,
            sender='manual-upload@upload.local',
            sender_name=customer_label or user.get_full_name() or user.get_username(),
            subject=_truncated((subject or '').strip() or f'Manual upload: {first_name}', 998),
            body_text='\n'.join(body_lines),
            received_at=timezone.now(),
            status=TrackedEmail.STATUS_PENDING,
            uploaded_by=user,
            upload_customer=customer,
            upload_salesman=salesman,
            upload_customer_name=(customer_display_name or customer_label)[:255],
            upload_salesman_name=salesman_label[:255],
        )
        for f in files:
            data = f.read()
            attachment = EmailAttachment(
                tracked_email=tracked_email,
                filename=_truncated(f.name, 255),
                content_type=content_type_for(f.name),
                size_bytes=len(data),
                file_hash=sha256_hex(data),
                included_in_classification=len(data) <= settings.EMAILAGENT_MAX_ATTACHMENT_MB * 1024 * 1024,
            )
            attachment.file.save(f.name, ContentFile(data), save=False)
            attachment.save()
    return tracked_email


def start_processing(tracked_email_id):
    """Runs the classify + draft steps in the background (they call Claude and can take a
    minute or more), so the upload request returns immediately."""
    threading.Thread(target=_run, args=(tracked_email_id,), daemon=True).start()


def _run(tracked_email_id):
    close_old_connections()
    try:
        process_upload(tracked_email_id)
    except Exception:
        logger.exception(f'Manual upload processing crashed for TrackedEmail {tracked_email_id}')
        try:
            _mark_failed(TrackedEmail.objects.get(pk=tracked_email_id), 'Processing failed unexpectedly -- see server log.')
        except Exception:
            logger.exception('Could not record the upload failure')
    finally:
        close_old_connections()


def _mark_failed(tracked_email, message):
    tracked_email.status = TrackedEmail.STATUS_NEEDS_REVIEW
    tracked_email.save(update_fields=['status'])
    draft, _ = QuotationDraft.objects.get_or_create(tracked_email=tracked_email)
    if draft.status != QuotationDraft.STATUS_CONFIRMED:
        draft.status = QuotationDraft.STATUS_FAILED
        draft.error = message
        draft.save(update_fields=['status', 'error'])


# ── the pipeline ────────────────────────────────────────────────────────

def process_upload(tracked_email_id):
    tracked_email = TrackedEmail.objects.get(pk=tracked_email_id)

    payloads = []
    for att in tracked_email.attachments.all():
        data = None
        if att.included_in_classification and att.file:
            with att.file.open('rb') as fh:
                data = fh.read()
        payloads.append({
            'filename': att.filename,
            'content_type': att.content_type,
            'size_bytes': att.size_bytes,
            'data': data,
            'included_in_classification': bool(data),
        })
    parsed = {
        'sender': tracked_email.sender,
        'sender_name': tracked_email.sender_name,
        'subject': tracked_email.subject,
        'body_text': tracked_email.body_text,
    }

    classify_recorder = supervisor.AgentRunRecorder(AgentRun.AGENT_CLASSIFY, tracked_email=tracked_email)
    try:
        result = classifier.classify_email(parsed, payloads)
        if not result.items:
            # Same single retry process_new_message does for a real-content / empty-items slip.
            retry = classifier.classify_email(parsed, payloads)
            if retry.items:
                result = retry
    except Exception as exc:
        classify_recorder.fail(exc)
        classify_recorder.finish()
        raise

    classify_recorder.finish(
        summary=f"manual upload: category={result.category} confidence={result.confidence:.2f} "
                f"items={len(result.items)} model={result.model_used}",
        issues=[] if result.items else ["No requirement items were extracted from the uploaded file(s)."],
    )

    # The person uploaded this to be quoted, so it is an RFQ whenever items were found,
    # whatever the email-oriented classifier thought of it.
    with transaction.atomic():
        tracked_email.classification_category = result.category
        tracked_email.classification_confidence = result.confidence
        tracked_email.classification_reasoning = result.reasoning
        tracked_email.classified_at = timezone.now()
        tracked_email.classification_model = result.model_used
        tracked_email.status = TrackedEmail.STATUS_RFQ if result.items else TrackedEmail.STATUS_NEEDS_REVIEW
        tracked_email.save()
        EnquiryItem.objects.bulk_create([
            EnquiryItem(
                tracked_email=tracked_email,
                description=item.get('description', ''),
                category=_truncated(item.get('category', ''), 100),
                brand=_truncated(item.get('brand', ''), 255),
                quantity=_truncated(item.get('quantity', ''), 50),
                unit=_truncated(item.get('unit', ''), 50),
                notes=_truncated(item.get('notes', ''), 255),
                source_attachment=_truncated(item.get('source_attachment', ''), 255),
                order=i,
            )
            for i, item in enumerate(result.items)
            if item.get('description')
        ])

    if not result.items:
        _mark_failed(
            tracked_email,
            "No requirement items could be read from the uploaded file(s). Check the file, "
            "or add the quotation manually.",
        )
        return

    draft_recorder = supervisor.AgentRunRecorder(AgentRun.AGENT_DRAFT, tracked_email=tracked_email)
    try:
        quotation_agent.draft_quotation(tracked_email)
        supervisor.finalize_draft_run(draft_recorder, tracked_email)
    except Exception as exc:
        logger.exception(f'Quotation drafting failed for uploaded TrackedEmail {tracked_email.id}')
        draft_recorder.fail(exc)
        draft_recorder.finish()
        _mark_failed(tracked_email, f'Quotation drafting failed: {exc}')
        return

    try:
        _apply_typed_names(tracked_email)
    except Exception:
        logger.exception(f'Applying typed customer/salesman failed for TrackedEmail {tracked_email.id}')


# ── typed customer / salesman ───────────────────────────────────────────

def find_customer(text):
    """Best catalog customer for typed text: exact name or code, else a single partial
    match (every word present). Returns None when nothing, or more than one, fits."""
    from so.models import Customer

    text = (text or '').strip()
    if not text:
        return None
    exact = (Customer.objects.filter(customer_name__iexact=text).first()
             or Customer.objects.filter(customer_code__iexact=text).first())
    if exact:
        return exact
    qs = Customer.objects.all()
    for word in text.split():
        qs = qs.filter(customer_name__icontains=word)
    found = list(qs[:2])
    return found[0] if len(found) == 1 else None


def find_salesman(text):
    from so.models import Salesman

    text = (text or '').strip()
    if not text:
        return None
    exact = Salesman.objects.filter(salesman_name__iexact=text).first()
    if exact:
        return exact
    qs = Salesman.objects.all()
    for word in text.split():
        qs = qs.filter(salesman_name__icontains=word)
    found = list(qs[:2])
    return found[0] if len(found) == 1 else None


def _apply_typed_names(tracked_email):
    """Applies what was chosen on the upload form to the drafted quotation(s): the picked
    customer / salesman win over whatever the agent guessed; a typed display name is shown on
    the quotation (walk-in / cash customers)."""
    picked_customer = tracked_email.upload_customer
    picked_salesman = tracked_email.upload_salesman
    display_name = tracked_email.upload_customer_name.strip()
    if not (picked_customer or picked_salesman or display_name):
        return

    draft = QuotationDraft.objects.filter(tracked_email=tracked_email).select_related('quotation').first()
    quotations = []
    if draft and draft.quotation:
        quotations.append(draft.quotation)
    for extra in tracked_email.additional_quotation_drafts.select_related('quotation'):
        if extra.quotation:
            quotations.append(extra.quotation)
    if not quotations:
        return

    from so.models import Customer

    customer = picked_customer
    walk_in = None
    if not customer and display_name:
        # Only a name was typed: use a catalog customer when exactly one fits, else walk-in.
        customer = find_customer(display_name)
        if not customer:
            walk_in = Customer.objects.filter(customer_name=quotation_agent.WALKIN_CUSTOMER_NAME).first()

    for q in quotations:
        notes = []
        if customer:
            q.customer = customer
            same = display_name.lower() == customer.customer_name.lower()
            q.customer_display_name = display_name[:255] if display_name and not same else None
        elif walk_in:
            q.customer = walk_in
            q.customer_display_name = display_name[:255]
            notes.append(f'Customer "{display_name}" was not found (or matched several) in the customer '
                         'list -- shown by the typed name under the walk-in customer; pick the right customer when editing.')
        if picked_salesman:
            q.salesman = picked_salesman
        elif customer and customer.salesman_id:
            q.salesman = customer.salesman
        if notes:
            q.remarks = (q.remarks or '') + '\n\n' + '\n'.join(notes)
        q.save()

    if draft and customer:
        draft.matched_customer = customer
        draft.save(update_fields=['matched_customer'])


# ── status for the progress page ────────────────────────────────────────

def upload_state(tracked_email):
    """Returns {'state': 'processing'|'done'|'failed', ...} for the progress page."""
    draft = QuotationDraft.objects.filter(tracked_email=tracked_email).select_related('quotation').first()
    if draft and draft.status == QuotationDraft.STATUS_CONFIRMED and draft.quotation_id:
        extras = [a.quotation for a in tracked_email.additional_quotation_drafts.select_related('quotation') if a.quotation]
        return {
            'state': 'done',
            'quotations': [draft.quotation] + extras,
            'draft_id': draft.pk,
            'item_count': tracked_email.items.count(),
        }
    if draft and draft.status == QuotationDraft.STATUS_FAILED:
        return {'state': 'failed', 'error': draft.error or 'The quotation could not be drafted.', 'draft_id': draft.pk}
    age = timezone.now() - tracked_email.created_at
    if age.total_seconds() > STALLED_AFTER_MINUTES * 60:
        return {'state': 'failed', 'error': 'Processing did not finish. Please upload the file again.'}
    return {'state': 'processing'}
