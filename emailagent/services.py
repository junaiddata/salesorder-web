"""Orchestration: gmail_client (fetch) -> classifier (classify) -> DB
(persist) -> quotation_agent (draft a quotation for RFQs). No Gmail API
calls or Claude calls happen outside gmail_client.py / classifier.py /
quotation_agent.py -- this module wires them together.
"""
import logging
import re

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import classifier, gmail_client, lpo_agent, outlook_client, quotation_agent, supervisor
from .models import (
    AgentRun, EmailAgentSyncState, EmailAttachment, EnquiryItem, QuotationDraft, SubmittalRequestItem, TrackedEmail,
)

logger = logging.getLogger(__name__)

_MESSAGE_ID_RE = re.compile(r'<[^<>]+>')


def _truncated(value, max_length):
    """Clip an AI-extracted value to a CharField's max_length before it hits
    the DB. Without this, a single overlong field (the model occasionally
    stuffs a full sentence into e.g. 'quantity' or 'notes' instead of a short
    value) raises a StringDataRightTruncation from Postgres -- and since this
    is called inside the same transaction.atomic() block that creates the
    TrackedEmail itself, that error rolls back the whole email, silently
    losing it rather than just trimming one field (see incidents processing
    Outlook UID 786281 and 786503)."""
    value = value or ''
    return value[:max_length] if len(value) > max_length else value


def _reply_header_message_ids(raw_headers):
    """Message-IDs cited by this message's own In-Reply-To/References
    headers -- shared by both reply-linking fallbacks below."""
    message_ids = set()
    for header in raw_headers:
        name = (header.get('name') or '').lower()
        if name in ('in-reply-to', 'references'):
            message_ids.update(_MESSAGE_ID_RE.findall(header.get('value') or ''))
    return message_ids


def _find_quotation_by_reply_headers(raw_headers):
    """Gmail's own thread_id is the primary way a customer's reply gets
    linked back to its enquiry (see is_thread_continuation below), but that
    link breaks whenever OUR OWN outbound "Send Quotation" email isn't part
    of the Gmail thread it replied to -- e.g. it was sent via plain SMTP
    rather than through the watched Gmail account/API, so Gmail has no
    record of it and starts a new thread for the client's reply (this is a
    real, observed failure mode: a customer replying "please change brand
    to X" to our quotation email got treated as a brand-new enquiry and
    quoted twice). As a fallback, every "Send Quotation" send records its
    own Message-ID on the Quotation (Quotation.emailed_message_id) -- if
    this reply's In-Reply-To/References headers cite that Message-ID, it's
    unambiguously a reply to that specific quotation email even though
    Gmail disagrees about the thread. Returns the matching Quotation, or
    None."""
    from so.models import Quotation

    message_ids = _reply_header_message_ids(raw_headers)
    if not message_ids:
        return None
    return Quotation.objects.filter(emailed_message_id__in=message_ids).order_by('-emailed_at').first()


def _find_tracked_email_by_reply_headers(raw_headers):
    """Same idea as _find_quotation_by_reply_headers, but matches this
    reply's In-Reply-To/References headers directly against OTHER
    TrackedEmails' own Message-IDs (TrackedEmail.gmail_message_id stores the
    RFC822 Message-ID header for Outlook/IMAP mail -- see
    outlook_client.parse_message) rather than against a quotation we already
    sent out. This is what links a CLIENT's own multi-part enquiry back to
    itself -- e.g. a first email with no usable content ("see attached BOQ"
    with a broken/missing attachment) followed by a corrected second email --
    for the Outlook/IMAP source, which has no native thread_id at all
    (outlook_client.parse_message always leaves it ''), so
    is_thread_continuation below would otherwise never fire for these and
    every follow-up in the same conversation would be (mis)treated as a
    brand-new, unrelated enquiry instead of being merged into/associated
    with the earlier one. Returns the most recently received matching
    TrackedEmail, or None."""
    message_ids = _reply_header_message_ids(raw_headers)
    if not message_ids:
        return None
    return TrackedEmail.objects.filter(gmail_message_id__in=message_ids).order_by('-received_at').first()


_INLINE_DECORATIVE_MAX_BYTES = 20 * 1024  # signature logos/social icons are
# reliably well under this; a genuine photo inserted inline (e.g. from a
# phone's camera picker instead of "Attach file") is not, so only skip small
# cid: images rather than every one.


def _build_attachment_payloads(service, gmail_message_id, parsed_attachments):
    """Fetches attachment bytes from Gmail (skipping oversized ones) and
    returns plain dicts consumed by both classifier.build_classification_content
    and (for a real run) EmailAttachment persistence."""
    max_bytes = settings.EMAILAGENT_MAX_ATTACHMENT_MB * 1024 * 1024
    payloads = []
    for att in parsed_attachments:
        size_bytes = att.get('size_bytes', 0) or 0
        is_image = att.get('content_type', '').startswith('image/')
        if is_image and att.get('content_id') and size_bytes <= _INLINE_DECORATIVE_MAX_BYTES:
            # Small image referenced via cid: in the HTML body -- almost
            # always a company logo, signature banner, or social icon rather
            # than content the sender deliberately shared -- skip entirely,
            # never fetched or stored.
            continue
        data = None
        if att.get('data') is not None:
            # IMAP/Outlook: a single full-message FETCH already returns every
            # part's decoded bytes -- no separate lazy-fetch step like Gmail's.
            data = att['data']
        elif att.get('inline_data'):
            data = gmail_client.decode_base64url(att['inline_data'])
        elif att.get('attachment_id') and size_bytes <= max_bytes:
            data = gmail_client.fetch_attachment_bytes(service, gmail_message_id, att['attachment_id'])
        payloads.append({
            'filename': att.get('filename', ''),
            'content_type': att.get('content_type', ''),
            'size_bytes': size_bytes,
            'gmail_attachment_id': att.get('attachment_id', ''),
            'data': data,
            'included_in_classification': bool(data) and size_bytes <= max_bytes,
        })
    return payloads


def process_new_message(service, message_id, dry_run=False, client=gmail_client, source=TrackedEmail.SOURCE_GMAIL,
                         submittal_only=False):
    """Returns a summary dict, or None if the message was already tracked
    (real run only -- dry runs always classify since nothing is persisted).

    `client` is either gmail_client or outlook_client -- both expose the same
    fetch_message(service, message_id) / parse_message(raw) contract and
    return the same plain-dict shape, so everything below this point is
    provider-agnostic. `message_id` is the provider's own per-message handle
    passed to fetch_message (Gmail's API message id, or an Outlook IMAP UID)
    -- NOT necessarily the same value as parsed['gmail_message_id'] (the
    dedup key stored on TrackedEmail), which is only known once the message
    has actually been parsed -- so the dedup check happens after parsing,
    not before.

    `submittal_only=True` (used for the separate project@junaid.ae mailbox --
    see poll_project_mailbox) still classifies and stores the email exactly
    like any other source -- an RFQ or LPO from that mailbox is still
    tracked/visible -- it just never triggers quotation drafting or LPO
    processing for it, regardless of what it classifies as. SubmittalRequestItem
    extraction and storage happen unconditionally either way (same as today),
    since that was never gated on `status` in the first place."""
    raw = client.fetch_message(service, message_id)
    parsed = client.parse_message(raw)

    if not dry_run and TrackedEmail.objects.filter(gmail_message_id=parsed['gmail_message_id']).exists():
        return None

    attachment_payloads = _build_attachment_payloads(service, parsed['gmail_message_id'], parsed['attachments'])

    # Not recorded during a dry run -- dry runs never persist anything else
    # either (see the docstring above), so an AgentRun for one would be a
    # permanent row with no real document behind it.
    classify_recorder = None if dry_run else supervisor.AgentRunRecorder(AgentRun.AGENT_CLASSIFY)
    retried_empty_items = False
    try:
        result = classifier.classify_email(parsed, attachment_payloads)
        # The model occasionally writes a reasoning paragraph describing real
        # requirement content (a BOQ in the body, or in an attached image/PDF)
        # but still returns an empty items list -- an instruction-following
        # slip on long/multi-attachment threads, not a genuine "nothing to
        # extract" case. One retry recovers most of these; if the retry is
        # also empty, that's surfaced below as an issue for a human to check
        # rather than silently accepted.
        if (result.category == classifier.CATEGORY_RFQ and not result.items
                and (attachment_payloads or len(parsed.get('body_text') or '') > 500)):
            retried_empty_items = True
            retry_result = classifier.classify_email(parsed, attachment_payloads)
            if retry_result.items:
                result = retry_result
    except Exception as exc:
        if classify_recorder:
            classify_recorder.fail(exc)
            classify_recorder.finish()
        raise
    status = classifier.decide_status(result.category, result.confidence)
    # decide_status only scores pricing intent, so a client asking purely for
    # submittal/technical-approval documents -- or sending purely their own
    # LPO/Purchase Order with no fresh pricing content -- resolves to
    # not_relevant and would otherwise be dropped below before ever reaching
    # the submittal-drafting / LPO-processing steps. Keep it, tagged
    # distinctly so it's easy to find in the UI. LPO wins when both flags
    # are set on an otherwise-not_relevant email, since a confirmed order is
    # higher-stakes than a submittal ask.
    if status == TrackedEmail.STATUS_NOT_RELEVANT:
        if result.is_lpo:
            status = TrackedEmail.STATUS_LPO
        elif result.is_submittal_request:
            status = TrackedEmail.STATUS_SUBMITTAL

    if classify_recorder:
        classify_issues = []
        if result.category == classifier.CATEGORY_UNCERTAIN:
            classify_issues.append("Classification uncertain -- needs manual review.")
        if retried_empty_items and not result.items:
            classify_issues.append(
                "Classified as RFQ with real content (body text and/or attachments) but zero "
                "items were extracted, even after an automatic retry -- check manually for a "
                "requirement/BOQ that may have been missed."
            )
        classify_recorder.finish(
            summary=f"category={result.category} confidence={result.confidence:.2f} items={len(result.items)}",
            issues=classify_issues,
        )

    # A reply or CC follow-up within an already-tracked enquiry thread belongs
    # to that enquiry regardless of how it classifies on its own (e.g. a short
    # "please see revised quantity" or "please quote in Cosmoplast instead"
    # reply has no RFQ language by itself, so the classifier may rate it
    # not_relevant or uncertain/needs_review) -- keep it stored AND route it
    # through the same merge pipeline as a confident RFQ, so a short brand-
    # change reply never gets stuck waiting on manual review instead of
    # updating the existing quotation (see merge_followup_into_quotation).
    # Fallback for when Gmail's thread_id doesn't link the reply back to its
    # enquiry (see _find_quotation_by_reply_headers) -- a match here is just
    # as strong a signal as a matching thread_id, so it's treated the same
    # way below. Also the ONLY signal at all for Outlook/IMAP mail, which has
    # no native thread_id (see _find_tracked_email_by_reply_headers).
    reply_quotation = None if dry_run else _find_quotation_by_reply_headers(parsed['raw_headers'])
    reply_tracked_email = None if dry_run else _find_tracked_email_by_reply_headers(parsed['raw_headers'])
    is_thread_continuation = not dry_run and bool(
        (parsed['thread_id'] and TrackedEmail.objects.filter(thread_id=parsed['thread_id']).exists())
        or reply_tracked_email
    )
    if (is_thread_continuation or reply_quotation) and status in (TrackedEmail.STATUS_NOT_RELEVANT, TrackedEmail.STATUS_NEEDS_REVIEW):
        status = TrackedEmail.STATUS_RFQ

    summary = {
        'gmail_message_id': parsed['gmail_message_id'],
        'subject': parsed['subject'],
        'sender': parsed['sender'],
        'category': result.category,
        'confidence': result.confidence,
        'reasoning': result.reasoning,
        'status': status,
        'stored': status != TrackedEmail.STATUS_NOT_RELEVANT,
        'item_count': len(result.items),
        'attachment_sources': result.attachment_sources,
    }
    if dry_run:
        return summary

    if status == TrackedEmail.STATUS_NOT_RELEVANT:
        # Only genuine client enquiries (rfq / needs_review) are kept -- a
        # confidently irrelevant email is classified but never persisted.
        return summary

    with transaction.atomic():
        tracked_email = TrackedEmail.objects.create(
            gmail_message_id=parsed['gmail_message_id'],
            thread_id=parsed['thread_id'],
            source=source,
            sender=parsed['sender'],
            sender_name=parsed['sender_name'],
            to_recipients=parsed['to'],
            cc_recipients=parsed['cc'],
            bcc_recipients=parsed['bcc'],
            subject=parsed['subject'],
            body_text=parsed['body_text'],
            body_html=parsed['body_html'],
            received_at=parsed['received_at'],
            raw_headers=parsed['raw_headers'],
            status=status,
            classification_category=result.category,
            classification_confidence=result.confidence,
            classification_reasoning=result.reasoning,
            classified_at=timezone.now(),
            classification_model=settings.EMAILAGENT_CLASSIFICATION_MODEL,
            submittal_brand=result.submittal_brand,
            submittal_project=result.submittal_project,
            submittal_client=result.submittal_client,
            submittal_consultant=result.submittal_consultant,
            submittal_main_contractor=result.submittal_main_contractor,
            submittal_mep_contractor=result.submittal_mep_contractor,
        )

        for payload in attachment_payloads:
            if result.attachment_sources.get(payload['filename']) == 'supplier_reference':
                # Our own previously-sent quotation/pricing document, kept in
                # the thread only for reference -- not the client's own
                # document, so it isn't stored.
                continue
            attachment = EmailAttachment(
                tracked_email=tracked_email,
                gmail_attachment_id=payload['gmail_attachment_id'],
                filename=payload['filename'],
                content_type=payload['content_type'],
                size_bytes=payload['size_bytes'],
                included_in_classification=payload['included_in_classification'],
            )
            if payload['data']:
                attachment.file.save(payload['filename'] or 'attachment', ContentFile(payload['data']), save=False)
            attachment.save()

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

        SubmittalRequestItem.objects.bulk_create([
            SubmittalRequestItem(
                tracked_email=tracked_email,
                description=item.get('description', ''),
                category=_truncated(item.get('category', ''), 100),
                brand=_truncated(item.get('brand', ''), 255),
                order=i,
            )
            for i, item in enumerate(result.submittal_items)
            if item.get('description')
        ])

    # The classify AgentRun above was recorded before tracked_email existed
    # -- attach it now so it shows up under this email's activity.
    classify_recorder.run.tracked_email = tracked_email
    classify_recorder.run.save(update_fields=['tracked_email'])

    # Kept as a stable reference to the classification result -- `result`
    # gets reused/reassigned below (to the merge outcome dict) once
    # quotation drafting starts, so this is grabbed first for the LPO step,
    # which needs the original is_lpo/lpo_* fields and runs after that block.
    classification_result = result

    if status == TrackedEmail.STATUS_RFQ and not submittal_only:
        # Outside the transaction above -- this calls out to Claude, and
        # shouldn't hold the enquiry's DB transaction open while it does.
        # Best-effort: draft_quotation/merge_followup_into_quotation never
        # raise, they record failures on the draft itself so a bad draft
        # never blocks email tracking.
        draft_recorder = supervisor.AgentRunRecorder(AgentRun.AGENT_DRAFT, tracked_email=tracked_email)

        # A same-thread follow-up (e.g. "wrong brand, please use X instead")
        # should correct the enquiry's existing quotation, not spin up a
        # second one for the same requirement -- find the email in this
        # thread that already has a live, agent-created quotation to merge
        # into, if any.
        original = None
        if is_thread_continuation:
            # Same-conversation candidates: anything sharing Gmail's
            # thread_id (when present), PLUS -- for Outlook/IMAP mail, which
            # has no thread_id at all -- the specific email this one's
            # In-Reply-To/References headers point to, and (one hop further)
            # anything sharing THAT email's own thread_id. This lets a
            # multi-part Outlook enquiry (e.g. a contentless first email
            # followed by one with the real BOQ, or a content-first email
            # followed by a brand/qty correction) resolve to the same
            # original enquiry either way, not just when Gmail's own
            # threading happens to catch it.
            thread_filter = Q()
            if tracked_email.thread_id:
                thread_filter |= Q(thread_id=tracked_email.thread_id)
            if reply_tracked_email:
                thread_filter |= Q(id=reply_tracked_email.id)
                if reply_tracked_email.thread_id:
                    thread_filter |= Q(thread_id=reply_tracked_email.thread_id)
            if thread_filter:
                original = (TrackedEmail.objects
                            .filter(thread_filter)
                            .exclude(id=tracked_email.id)
                            .filter(quotation_draft__status=QuotationDraft.STATUS_CONFIRMED,
                                    quotation_draft__quotation__isnull=False)
                            .order_by('-received_at')
                            .first())
        if not original and reply_quotation:
            # Thread-id lookup found nothing (or this email's thread has no
            # other tracked members at all) -- fall back to the reply-headers
            # match found above.
            source_draft = getattr(reply_quotation, 'quotation_draft', None)
            original = source_draft.tracked_email if source_draft else None

        try:
            if original:
                result = quotation_agent.merge_followup_into_quotation(tracked_email, original)
                QuotationDraft.objects.create(
                    tracked_email=tracked_email,
                    status=QuotationDraft.STATUS_MERGED if result['merged'] else QuotationDraft.STATUS_FAILED,
                    merged_into=result['quotation'],
                    error='' if result['merged'] else '; '.join(result['issues']),
                )
                # Both outcomes (merged -- including reopening an already-Approved
                # quotation back to Pending, see merge_followup_into_quotation --
                # or correctly declined because a human has actually edited/
                # discount-reviewed it) are FLAGGED rather than FAILED -- the
                # agent behaved correctly either way; a real crash is still
                # caught below.
                draft_recorder.finish(summary=result['summary'], issues=result['issues'], quotation=result['quotation'])
            else:
                quotation_agent.draft_quotation(tracked_email)
                supervisor.finalize_draft_run(draft_recorder, tracked_email)
        except Exception as exc:
            logger.exception(f"Quotation drafting failed for TrackedEmail {tracked_email.id}")
            draft_recorder.fail(exc)
            draft_recorder.finish()

    # Submittal drafting is no longer automatic here -- a submittal-request
    # email's items are saved above (SubmittalRequestItem) for a human to
    # pick from on the email detail page; see
    # emailagent.views.submittal_draft_selected /
    # submittal_agent.draft_submittals_for_selected_items.

    if classification_result.is_lpo and not submittal_only:
        # Unconditional on is_lpo, not on `status` -- same principle as the
        # EnquiryItem/SubmittalRequestItem persistence above: an LPO's data
        # must be captured even when the email's overall status ends up
        # 'rfq' or 'needs_review' (e.g. a fresh question arrives in the same
        # thread as a PO confirmation). Outside the transaction above since
        # this can write a real SalesOrder -- never raises, failures are
        # recorded on the LPORequest itself.
        # (submittal_only mailboxes never process LPOs either -- see this
        # function's docstring.)
        lpo_recorder = supervisor.AgentRunRecorder(AgentRun.AGENT_PROCESS_LPO, tracked_email=tracked_email)
        try:
            lpo_agent.process_lpo(tracked_email, classification_result)
            supervisor.finalize_lpo_run(lpo_recorder, tracked_email)
        except Exception as exc:
            logger.exception(f"LPO processing failed for TrackedEmail {tracked_email.id}")
            lpo_recorder.fail(exc)
            lpo_recorder.finish()

    return summary


def poll_gmail(dry_run=False, max_results=None):
    service = gmail_client.build_service()
    sync_state = EmailAgentSyncState.get_instance()

    message_ids, new_history_id, used_fallback = gmail_client.list_new_message_ids(
        service, sync_state.last_history_id,
    )
    if max_results:
        message_ids = message_ids[:max_results]

    stats = {
        'total': len(message_ids), 'processed': 0, 'skipped': 0, 'errors': 0,
        'used_fallback': used_fallback, 'results': [],
    }

    for message_id in message_ids:
        try:
            result = process_new_message(service, message_id, dry_run=dry_run,
                                          client=gmail_client, source=TrackedEmail.SOURCE_GMAIL)
            if result is None:
                stats['skipped'] += 1
            else:
                stats['processed'] += 1
                stats['results'].append(result)
        except Exception:
            stats['errors'] += 1
            logger.exception(f"Failed to process Gmail message {message_id}")

    if not dry_run:
        sync_state.last_history_id = new_history_id
        sync_state.last_synced_internal_date = timezone.now()
        sync_state.last_run_at = timezone.now()
        # update_fields is required, not optional: sync_state is a shared
        # singleton row and this poller loaded its copy before the other
        # mailboxes' pollers (which run concurrently, e.g. via the watch_*
        # commands) wrote their own watermarks -- a bare save() would write
        # back this stale in-memory copy of the WHOLE row and clobber their
        # updates. Scope the write to only the columns this poller owns.
        sync_state.save(update_fields=['last_history_id', 'last_synced_internal_date', 'last_run_at'])

    return stats


def poll_outlook(dry_run=False, max_results=None):
    """Same role as poll_gmail() but for the plain-IMAP mailbox (see
    outlook_client.py). Uses the IMAP UID watermark
    (EmailAgentSyncState.last_outlook_uid) instead of Gmail's historyId --
    see outlook_client.list_new_message_uids for why a never-before-polled
    mailbox does NOT walk its entire history here (this mailbox already
    holds 40k+ unrelated messages)."""
    conn = outlook_client.build_connection()
    try:
        sync_state = EmailAgentSyncState.get_instance()

        uids, new_last_uid, is_first_run = outlook_client.list_new_message_uids(
            conn, sync_state.last_outlook_uid, max_results=max_results,
        )

        stats = {
            'total': len(uids), 'processed': 0, 'skipped': 0, 'errors': 0,
            'used_fallback': is_first_run, 'results': [],
        }

        for uid in uids:
            try:
                result = process_new_message(conn, uid, dry_run=dry_run,
                                              client=outlook_client, source=TrackedEmail.SOURCE_OUTLOOK)
                if result is None:
                    stats['skipped'] += 1
                else:
                    stats['processed'] += 1
                    stats['results'].append(result)
            except Exception:
                stats['errors'] += 1
                logger.exception(f"Failed to process Outlook message UID {uid}")

        if not dry_run:
            sync_state.last_outlook_uid = new_last_uid
            sync_state.last_outlook_run_at = timezone.now()
            # update_fields: see the matching comment in poll_gmail -- this
            # is a shared singleton row polled concurrently by every mailbox.
            sync_state.save(update_fields=['last_outlook_uid', 'last_outlook_run_at'])

        return stats
    finally:
        outlook_client.close_connection(conn)


def poll_project_mailbox(dry_run=False, max_results=None):
    """Same role as poll_outlook() but for the separate project@junaid.ae
    IMAP mailbox -- submittal-only (see process_new_message's submittal_only
    param: this mailbox's mail is classified and stored exactly like any
    other source, it just never triggers quotation drafting or LPO
    processing). Uses its own PROJECT_IMAP_* credentials and its own UID
    watermark (EmailAgentSyncState.last_project_uid) so it advances
    independently of the primary Outlook mailbox's polling position."""
    conn = outlook_client.build_connection(
        host=settings.PROJECT_IMAP_HOST, port=settings.PROJECT_IMAP_PORT,
        user=settings.PROJECT_IMAP_USER, password=settings.PROJECT_IMAP_PASSWORD,
        folder=settings.PROJECT_IMAP_FOLDER,
    )
    try:
        sync_state = EmailAgentSyncState.get_instance()

        uids, new_last_uid, is_first_run = outlook_client.list_new_message_uids(
            conn, sync_state.last_project_uid, max_results=max_results,
        )

        stats = {
            'total': len(uids), 'processed': 0, 'skipped': 0, 'errors': 0,
            'used_fallback': is_first_run, 'results': [],
        }

        for uid in uids:
            try:
                result = process_new_message(conn, uid, dry_run=dry_run,
                                              client=outlook_client, source=TrackedEmail.SOURCE_PROJECT,
                                              submittal_only=True)
                if result is None:
                    stats['skipped'] += 1
                else:
                    stats['processed'] += 1
                    stats['results'].append(result)
            except Exception:
                stats['errors'] += 1
                logger.exception(f"Failed to process project-mailbox message UID {uid}")

        if not dry_run:
            sync_state.last_project_uid = new_last_uid
            sync_state.last_project_run_at = timezone.now()
            # update_fields: see the matching comment in poll_gmail -- this
            # is a shared singleton row polled concurrently by every mailbox.
            sync_state.save(update_fields=['last_project_uid', 'last_project_run_at'])

        return stats
    finally:
        outlook_client.close_connection(conn)


def poll_submittal_mailbox(dry_run=False, max_results=None):
    """Same role as poll_project_mailbox() but for the fourth, separate
    SUBMITTAL_IMAP_* mailbox -- submittal-only. Uses its own credentials and
    its own UID watermark (EmailAgentSyncState.last_submittal_uid) so it
    advances independently of every other mailbox's polling position."""
    conn = outlook_client.build_connection(
        host=settings.SUBMITTAL_IMAP_HOST, port=settings.SUBMITTAL_IMAP_PORT,
        user=settings.SUBMITTAL_IMAP_USER, password=settings.SUBMITTAL_IMAP_PASSWORD,
        folder=settings.SUBMITTAL_IMAP_FOLDER,
    )
    try:
        sync_state = EmailAgentSyncState.get_instance()

        uids, new_last_uid, is_first_run = outlook_client.list_new_message_uids(
            conn, sync_state.last_submittal_uid, max_results=max_results,
        )

        stats = {
            'total': len(uids), 'processed': 0, 'skipped': 0, 'errors': 0,
            'used_fallback': is_first_run, 'results': [],
        }

        for uid in uids:
            try:
                result = process_new_message(conn, uid, dry_run=dry_run,
                                              client=outlook_client, source=TrackedEmail.SOURCE_SUBMITTAL,
                                              submittal_only=True)
                if result is None:
                    stats['skipped'] += 1
                else:
                    stats['processed'] += 1
                    stats['results'].append(result)
            except Exception:
                stats['errors'] += 1
                logger.exception(f"Failed to process submittal-mailbox message UID {uid}")

        if not dry_run:
            sync_state.last_submittal_uid = new_last_uid
            sync_state.last_submittal_run_at = timezone.now()
            # update_fields: see the matching comment in poll_gmail -- this
            # is a shared singleton row polled concurrently by every mailbox.
            sync_state.save(update_fields=['last_submittal_uid', 'last_submittal_run_at'])

        return stats
    finally:
        outlook_client.close_connection(conn)
