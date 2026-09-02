import base64
import json
import logging
import threading
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import FileResponse, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import services
from .models import AgentRun, EmailAttachment, LPORequest, QuotationDraft, SubmittalDraft, TrackedEmail

logger = logging.getLogger(__name__)


@login_required
def review_queue(request):
    emails = (TrackedEmail.objects
              .filter(status=TrackedEmail.STATUS_NEEDS_REVIEW)
              .prefetch_related('attachments')
              .order_by('-received_at'))
    return render(request, 'emailagent/review.html', {'emails': emails})


@login_required
@require_POST
def review_confirm(request, pk):
    tracked_email = get_object_or_404(TrackedEmail, pk=pk)
    tracked_email.status = TrackedEmail.STATUS_RFQ
    tracked_email.confirmed_by = request.user
    tracked_email.confirmed_at = timezone.now()
    tracked_email.save(update_fields=['status', 'confirmed_by', 'confirmed_at'])
    messages.success(request, f'Marked "{tracked_email.subject or "(no subject)"}" as an RFQ.')
    return redirect('emailagent:review_queue')


@login_required
@require_POST
def review_reject(request, pk):
    tracked_email = get_object_or_404(TrackedEmail, pk=pk)
    tracked_email.status = TrackedEmail.STATUS_NOT_RELEVANT
    tracked_email.confirmed_by = request.user
    tracked_email.confirmed_at = timezone.now()
    tracked_email.save(update_fields=['status', 'confirmed_by', 'confirmed_at'])
    messages.success(request, f'Marked "{tracked_email.subject or "(no subject)"}" as not relevant.')
    return redirect('emailagent:review_queue')


@login_required
def email_list(request):
    # A follow-up merged into an earlier email's quotation (see
    # emailagent.quotation_agent.merge_followup_into_quotation) isn't its own
    # enquiry -- exclude it here so the list shows one row per actual
    # enquiry rather than the follow-up alongside the (now-updated) original.
    # It's still visible via the original email's thread view and on the
    # Agent Activity / quotation-drafts pages, so nothing is actually hidden
    # from the audit trail -- only decluttered from this summary list.
    emails = (TrackedEmail.objects.all()
              .select_related('quotation_draft', 'quotation_draft__merged_into')
              .exclude(quotation_draft__status=QuotationDraft.STATUS_MERGED)
              .prefetch_related('items', 'attachments')
              .order_by('-received_at'))
    status_filter = request.GET.get('status', '')
    if status_filter in dict(TrackedEmail.STATUS_CHOICES):
        emails = emails.filter(status=status_filter)
    return render(request, 'emailagent/email_list.html', {
        'emails': emails,
        'status_filter': status_filter,
    })


@login_required
def email_detail(request, pk):
    tracked_email = get_object_or_404(
        TrackedEmail.objects
        .select_related('lpo_request', 'lpo_request__matched_quotation', 'lpo_request__sales_order')
        .prefetch_related(
            'attachments', 'items', 'submittal_request_items',
            'lpo_request__items', 'lpo_request__candidate_quotations',
        ),
        pk=pk,
    )
    thread_emails = []
    if tracked_email.thread_id:
        thread_emails = (TrackedEmail.objects
                          .filter(thread_id=tracked_email.thread_id)
                          .exclude(pk=tracked_email.pk)
                          .order_by('received_at'))
    return render(request, 'emailagent/email_detail.html', {
        'email': tracked_email,
        'thread_emails': thread_emails,
    })


@login_required
@require_POST
def submittal_draft_selected(request, pk):
    """'Generate Submittal' action on the email detail page's Requested
    Submittal Items table -- the only entry point for a PURE submittal-
    request email (no pricing ask, so no Quotation ever gets drafted for
    it -- see quotation_agent.draft_quotation, which only runs for
    status='rfq' emails), so there's no quotation-page button to use
    instead. Drafts a submittal only for the items a human checked (see
    submittal_agent.draft_submittals_for_selected_items). Safe to call
    more than once per email as further batches of items get selected
    later -- items already attached to a drafted submittal are excluded
    automatically."""
    from . import submittal_agent
    from .models import AgentRun
    from .supervisor import AgentRunRecorder

    tracked_email = get_object_or_404(TrackedEmail, pk=pk)
    item_ids = request.POST.getlist('item_ids')
    if not item_ids:
        messages.warning(request, 'Select at least one item before generating a submittal.')
        return redirect('emailagent:email_detail', pk=pk)

    recorder = AgentRunRecorder(AgentRun.AGENT_DRAFT_SUBMITTAL, tracked_email=tracked_email)
    try:
        results = submittal_agent.draft_submittals_for_selected_items(tracked_email, item_ids)
    except Exception as exc:
        logger.exception(f"submittal_draft_selected failed for TrackedEmail {pk}")
        recorder.fail(exc)
        recorder.finish()
        messages.error(request, f'Could not generate a submittal for the selected items: {exc}')
        return redirect('emailagent:email_detail', pk=pk)

    created = [s for s in results if s]
    failed_count = len(results) - len(created)
    if created:
        recorder.finish(
            summary=f"drafted {len(created)} submittal(s) from {len(item_ids)} selected item(s)",
            submittal=created[0],
        )
        messages.success(request, f'Generated {len(created)} submittal(s) for review from the selected items.')
    else:
        recorder.fail("No submittal could be drafted for the selected items -- see the email page for details.")
        recorder.finish()
        messages.error(request, 'Could not generate a submittal for the selected items -- see below for why.')
    if created and failed_count:
        messages.warning(
            request,
            f'{failed_count} of the selected brand group(s) could not be matched to a brand in the '
            'submittal materials library.',
        )

    return redirect('emailagent:email_detail', pk=pk)


@login_required
def lpo_request_queue(request):
    """Audit list of LPO/Purchase Order emails the agent has processed --
    auto-created a Sales Order (confirmed), or left needing a human to
    match/complete (needs_review/failed), or dismissed as no-action-needed.
    Editing/creating a Sales Order from a needs-review LPO happens on
    lpo_request_review, not here."""
    lpo_requests = (LPORequest.objects
                     .select_related('tracked_email', 'matched_quotation', 'sales_order')
                     .order_by('-created_at'))
    return render(request, 'emailagent/lpo_queue.html', {'lpo_requests': lpo_requests})


@login_required
def lpo_request_review(request, pk):
    """Detail/manual-completion page for one LPORequest -- shows the
    extracted fields + line items, a link to the source PDF (via the
    existing attachment_download view), the matching outcome/reasoning,
    and (when not already confirmed) a form to pick a candidate quotation
    and create the Sales Order, or dismiss the LPO as no action needed."""
    from .models import StockShortageReport

    lpo_request = get_object_or_404(
        LPORequest.objects
        .select_related('tracked_email', 'matched_quotation', 'sales_order', 'source_attachment')
        .prefetch_related('items', 'candidate_quotations', 'candidate_quotations__customer'),
        pk=pk,
    )
    # Only fetched if it already exists -- viewing this page must never be
    # what creates the singleton report row.
    stock_shortage_report = StockShortageReport.objects.filter(pk=1).first()
    return render(request, 'emailagent/lpo_review.html', {
        'lpo_request': lpo_request,
        'stock_shortage_report': stock_shortage_report,
    })


@login_required
@require_POST
def lpo_request_convert(request, pk):
    """Human-triggered completion of a needs-review LPO -- picks a
    quotation (from the candidates offered, or any quotation_id posted)
    and converts it via the SAME shared service the automatic exact-match
    path uses (so.quotation_conversion_service), so a manual match behaves
    identically to an automatic one. Re-checks eligibility here rather than
    trusting whatever was true when the candidate list was rendered, since
    time may have passed (e.g. someone else approved/converted it since)."""
    from so import quotation_conversion_service
    from so.models import Quotation

    from .supervisor import AgentRunRecorder

    lpo_request = get_object_or_404(LPORequest, pk=pk)
    quotation_id = request.POST.get('quotation_id')
    if not quotation_id:
        messages.warning(request, 'Select a quotation before creating a sales order.')
        return redirect('emailagent:lpo_request_review', pk=pk)

    quotation = get_object_or_404(Quotation, id=quotation_id)
    recorder = AgentRunRecorder(AgentRun.AGENT_PROCESS_LPO, tracked_email=lpo_request.tracked_email, quotation=quotation)

    try:
        username = request.user.username if request.user.is_authenticated else None
        sales_order = quotation_conversion_service.convert_quotation_to_sales_order(quotation, username=username)
    except ValueError as e:
        recorder.fail(str(e))
        recorder.finish()
        messages.error(request, str(e))
        return redirect('emailagent:lpo_request_review', pk=pk)
    except Exception as exc:
        logger.exception(f"lpo_request_convert failed for LPORequest {pk}")
        recorder.fail(exc)
        recorder.finish()
        messages.error(request, f'Could not create a sales order: {exc}')
        return redirect('emailagent:lpo_request_review', pk=pk)

    lpo_request.matched_quotation = quotation
    lpo_request.sales_order = sales_order
    lpo_request.status = LPORequest.STATUS_CONFIRMED
    lpo_request.reviewed_by = request.user
    lpo_request.reviewed_at = timezone.now()
    lpo_request.match_reasoning = (
        f"{lpo_request.match_reasoning}\n\nManually matched to {quotation.quotation_number} and "
        f"converted to Sales Order {sales_order.order_number} by {request.user.username}."
    ).strip()
    lpo_request.save()

    recorder.finish(
        summary=f"lpo={lpo_request.lpo_number or '—'} matched={quotation.quotation_number} sales_order={sales_order.order_number} (manual)",
        sales_order=sales_order,
    )
    messages.success(request, f'Created Sales Order {sales_order.order_number} from {quotation.quotation_number}.')

    # Best-effort/non-blocking -- see stock_check's own docstring.
    from . import stock_check

    shortage_report = stock_check.run_stock_check_for_sales_order(sales_order)
    if shortage_report.lines:
        messages.warning(
            request,
            f'Stock shortage detected -- {len(shortage_report.lines)} item(s) need procurement '
            f'across all pending LPO orders. See the consolidated Stock Shortage Report.',
        )
        return redirect('emailagent:stock_shortage_report')

    return redirect('view_sales_order_details', order_id=sales_order.id)


@login_required
@require_POST
def lpo_request_recheck_match(request, pk):
    """Re-runs matching (emailagent.lpo_agent.match_and_maybe_convert)
    against an LPORequest's ALREADY-extracted fields/items -- for a
    needs-review LPO whose matching quotation didn't exist (or wasn't
    eligible) when it first arrived, but does/is now. Never re-reads the
    source email/PDF or calls Claude; only re-scores against the current
    state of so.Quotation. If exactly one confident candidate is now
    found and it's eligible, this auto-creates the Sales Order (tagged
    created_via='agent_lpo') exactly as if it had matched on first
    arrival -- otherwise it just refreshes the candidate list/reasoning
    shown on this page."""
    from .lpo_agent import match_and_maybe_convert
    from .supervisor import AgentRunRecorder

    lpo_request = get_object_or_404(LPORequest, pk=pk)
    if lpo_request.status not in (LPORequest.STATUS_NEEDS_REVIEW, LPORequest.STATUS_FAILED):
        messages.warning(request, 'This LPO is not in a state that can be re-checked.')
        return redirect('emailagent:lpo_request_review', pk=pk)

    recorder = AgentRunRecorder(AgentRun.AGENT_PROCESS_LPO, tracked_email=lpo_request.tracked_email)
    try:
        lpo_request.error = ''
        match_and_maybe_convert(lpo_request)
    except Exception as exc:
        logger.exception(f"lpo_request_recheck_match failed for LPORequest {pk}")
        recorder.fail(exc)
        recorder.finish()
        messages.error(request, f'Could not re-check the match: {exc}')
        return redirect('emailagent:lpo_request_review', pk=pk)

    if lpo_request.status == LPORequest.STATUS_CONFIRMED:
        recorder.finish(
            summary=(f"lpo={lpo_request.lpo_number or '—'} matched={lpo_request.matched_quotation.quotation_number} "
                      f"sales_order={lpo_request.sales_order.order_number} (auto, re-checked)"),
            quotation=lpo_request.matched_quotation, sales_order=lpo_request.sales_order,
        )
        messages.success(
            request,
            f'Match found -- auto-created Sales Order {lpo_request.sales_order.order_number} from '
            f'{lpo_request.matched_quotation.quotation_number}.',
        )
    else:
        recorder.finish(
            summary=f"lpo={lpo_request.lpo_number or '—'} -- re-checked, still needs review ({lpo_request.get_match_method_display()})",
            issues=[lpo_request.match_reasoning] if lpo_request.match_reasoning else [],
            quotation=lpo_request.matched_quotation,
        )
        messages.info(request, 'Re-checked -- still needs manual review (see the updated reasoning below).')
    return redirect('emailagent:lpo_request_review', pk=pk)


@login_required
@require_POST
def lpo_request_dismiss(request, pk):
    """Marks an LPORequest as reviewed with no action needed -- e.g. a
    duplicate PO, or a false-positive detection -- so it doesn't sit in
    the queue looking unresolved forever. Only valid from a state that
    actually needs a decision; never overwrites an already-confirmed
    (Sales Order created) request."""
    lpo_request = get_object_or_404(LPORequest, pk=pk)
    if lpo_request.status not in (LPORequest.STATUS_NEEDS_REVIEW, LPORequest.STATUS_FAILED):
        messages.warning(request, 'This LPO is not in a state that can be dismissed.')
        return redirect('emailagent:lpo_request_review', pk=pk)

    lpo_request.status = LPORequest.STATUS_DISMISSED
    lpo_request.dismissed_by = request.user
    lpo_request.dismissed_at = timezone.now()
    lpo_request.dismiss_reason = (request.POST.get('dismiss_reason') or '')[:255]
    lpo_request.save()
    messages.success(request, 'LPO marked as reviewed -- no action needed.')
    return redirect('emailagent:lpo_request_queue')


@login_required
def stock_shortage_report(request):
    """The single, always-current consolidated stock shortage picture
    (see emailagent.models.StockShortageReport -- a singleton, not one
    row per check) across every pending LPO-sourced sales order. `lines`
    (item_code/brand/description/total_required_qty/available_qty/
    final_qty/lpo_breakdown) is computed by emailagent.stock_check."""
    from .models import StockShortageReport

    report = (StockShortageReport.objects
              .select_related('last_triggered_by', 'last_triggered_by__customer')
              .filter(pk=1)
              .first()) or StockShortageReport.current()
    return render(request, 'emailagent/stock_shortage_detail.html', {'report': report})


@login_required
@require_POST
def stock_shortage_report_refresh(request):
    """Manually re-runs the consolidated check (see stock_check.recompute)
    with no specific triggering order -- useful after fulfilling/marking
    orders 'SO Created' (which drops them out of the pending demand pool)
    or after a fresh Items.total_available_stock sync, without waiting
    for the next LPO to arrive."""
    from . import stock_check

    stock_check.recompute()
    messages.success(request, 'Stock shortage report refreshed.')
    return redirect('emailagent:stock_shortage_report')


@login_required
def attachment_download(request, pk):
    attachment = get_object_or_404(EmailAttachment, pk=pk)
    return FileResponse(
        attachment.file.open('rb'),
        as_attachment=False,
        filename=attachment.filename or f'attachment-{attachment.pk}',
    )


@login_required
def quotation_draft_queue(request):
    """Audit list of quotations the agent has auto-created from RFQ emails
    (plus any still drafting or that failed) -- editing happens on the real
    quotation via the app's normal edit screen, not here. A follow-up
    merged into an earlier email's quotation is excluded -- it isn't its
    own quotation, so it would just show up as a confusing duplicate row
    next to the one it was folded into (same reasoning as email_list)."""
    drafts = (QuotationDraft.objects
              .select_related('tracked_email', 'matched_customer', 'quotation', 'merged_into')
              .exclude(status=QuotationDraft.STATUS_MERGED)
              .prefetch_related('tracked_email__items', 'quotation__items')
              .order_by('-tracked_email__received_at'))
    return render(request, 'emailagent/quotation_queue.html', {'drafts': drafts})


@login_required
def quotation_draft_review(request, pk):
    """Auto-created quotations are edited through the app's own quotation
    edit screen -- this just forwards there, or shows status while still
    drafting / if drafting failed (no quotation to forward to yet)."""
    draft = get_object_or_404(
        QuotationDraft.objects.select_related('tracked_email', 'quotation', 'merged_into'), pk=pk,
    )
    if draft.status == QuotationDraft.STATUS_CONFIRMED and draft.quotation_id:
        return redirect('edit_quotation', quotation_id=draft.quotation_id)
    if draft.status == QuotationDraft.STATUS_MERGED and draft.merged_into_id:
        return redirect('edit_quotation', quotation_id=draft.merged_into_id)
    return render(request, 'emailagent/quotation_draft_status.html', {'draft': draft})


@login_required
def submittal_draft_queue(request):
    """Audit list of submittals the agent has auto-drafted -- either from an
    email that asked for material submittal/technical-approval documents
    (tracked_email set), or via the 'Generate Submittal' action on an
    existing quotation (source_quotation set instead). Editing happens on
    the real submittal via the normal submittal wizard, not here -- every
    agent-created submittal starts at needs_review (see
    submittal.models.Submittal.needs_verification) until a human verifies it."""
    drafts = (SubmittalDraft.objects
              .select_related('tracked_email', 'source_quotation', 'source_quotation__customer',
                               'matched_brand', 'submittal')
              .order_by('-created_at'))
    return render(request, 'emailagent/submittal_queue.html', {'drafts': drafts})


_STATUS_RANK = {AgentRun.STATUS_FAILED: 2, AgentRun.STATUS_FLAGGED: 1, AgentRun.STATUS_SUCCESS: 0}
_DAYS_CHOICES = {'7': 7, '30': 30, '90': 90}


@login_required
def agent_activity(request):
    """Observability dashboard over every automated step in the RFQ
    pipeline (email classification, quotation drafting, unmatched-item
    rematch, brand recheck, and a quotation actually being emailed to a
    client) -- what ran, how long it took, and anything worth a human
    double-checking. Purely informational: nothing here edits an email or
    quotation, it only ever reflects what those steps already did.

    Default view groups runs by the document (RFQ email) they belong to, so
    every stage that touched that email/quotation shows up together with
    its own status and timestamp: this page alone should answer "what has
    the agent pipeline done with this enquiry" without needing to open the
    email/quotation screens separately. A flat, ungrouped log is also
    available (?view=log) for scanning everything chronologically.

    Filters (?agent=, ?status=, ?days=, ?q=) and pagination (?page=) all
    combine and keep the URL shareable/bookmarkable. Page size and the
    by-enquiry grouping cap are configurable via EMAILAGENT_ACTIVITY_PAGE_SIZE
    / EMAILAGENT_ACTIVITY_MAX_ROWS (settings.py / .env) -- as the AgentRun
    table grows, narrowing the date range keeps this page fast without
    needing a code change."""
    from django.core.paginator import Paginator

    runs = AgentRun.objects.select_related('tracked_email', 'quotation', 'submittal').all()

    agent_filter = request.GET.get('agent', '')
    if agent_filter in dict(AgentRun.AGENT_CHOICES):
        runs = runs.filter(agent_name=agent_filter)

    status_filter = request.GET.get('status', '')
    if status_filter in dict(AgentRun.STATUS_CHOICES):
        runs = runs.filter(status=status_filter)

    days_filter = request.GET.get('days', '')
    if days_filter in _DAYS_CHOICES:
        runs = runs.filter(created_at__gte=timezone.now() - timedelta(days=_DAYS_CHOICES[days_filter]))

    query = request.GET.get('q', '').strip()
    if query:
        runs = runs.filter(
            Q(tracked_email__subject__icontains=query)
            | Q(tracked_email__sender__icontains=query)
            | Q(tracked_email__sender_name__icontains=query)
            | Q(quotation__quotation_number__icontains=query)
            | Q(submittal__project__icontains=query)
            | Q(summary__icontains=query)
        )

    view = 'log' if request.GET.get('view') == 'log' else 'document'
    page_size = settings.EMAILAGENT_ACTIVITY_PAGE_SIZE

    base_params = request.GET.copy()
    base_params.pop('page', None)
    base_query = base_params.urlencode()

    total_counts = AgentRun.objects.aggregate(
        success=Count('id', filter=Q(status=AgentRun.STATUS_SUCCESS)),
        flagged=Count('id', filter=Q(status=AgentRun.STATUS_FLAGGED)),
        failed=Count('id', filter=Q(status=AgentRun.STATUS_FAILED)),
    )

    # Only offer agents that have actually run at least once -- not every
    # possible AGENT_CHOICES entry -- so the filter never lists an agent
    # that would just return an empty result.
    existing_agent_names = set(AgentRun.objects.values_list('agent_name', flat=True).distinct())
    agent_choices = [
        (value, label) for value, label in AgentRun.AGENT_CHOICES if value in existing_agent_names
    ]

    context = {
        'agent_filter': agent_filter,
        'status_filter': status_filter,
        'days_filter': days_filter,
        'query': query,
        'agent_choices': agent_choices,
        'status_choices': AgentRun.STATUS_CHOICES,
        'total_counts': total_counts,
        'view': view,
        'base_query': base_query,
    }

    if view == 'log':
        paginator = Paginator(runs.order_by('-created_at'), page_size)
        page_obj = paginator.get_page(request.GET.get('page'))
        context['page_obj'] = page_obj
        context['runs'] = page_obj.object_list
        return render(request, 'emailagent/agent_activity.html', context)

    # Group into one row per tracked_email, each holding its ordered stages.
    # Runs with no tracked_email (e.g. a dry run, or an email classified
    # not_relevant and never stored -- there's no document to group under)
    # fall back to their own list further down so they're never silently lost.
    recent_runs = list(runs.order_by('-started_at')[:settings.EMAILAGENT_ACTIVITY_MAX_ROWS])
    documents = {}
    orphans = []
    for run in recent_runs:
        if not run.tracked_email_id:
            orphans.append(run)
            continue
        doc = documents.setdefault(run.tracked_email_id, {
            'tracked_email': run.tracked_email,
            'stages': [],
            'latest': run.created_at,
            'worst_status': run.status,
        })
        doc['stages'].append(run)
        if run.created_at > doc['latest']:
            doc['latest'] = run.created_at
        if _STATUS_RANK[run.status] > _STATUS_RANK[doc['worst_status']]:
            doc['worst_status'] = run.status

    for doc in documents.values():
        doc['stages'].sort(key=lambda r: r.started_at)
        # Resulting submittal (if any), for the review-status chip -- read
        # straight off the stages rather than tracked_email.submittal_draft,
        # since a submittal generated from a QUOTATION (via 'Generate
        # Submittal', not this email directly) is still attributed to this
        # document when that quotation traces back to this enquiry thread
        # (see submittal_generate_from_quotation), but has no SubmittalDraft
        # pointed at this tracked_email.
        doc['submittal'] = next(
            (s.submittal for s in reversed(doc['stages']) if s.submittal_id), None,
        )
        # So the template can show one "N to review" disclosure per enquiry
        # instead of always rendering (and the human having to scan) every
        # stage's notes inline.
        doc['issue_count'] = sum(len(s.issues) + (1 if s.error else 0) for s in doc['stages'])

    document_rows = sorted(documents.values(), key=lambda d: d['latest'], reverse=True)

    doc_paginator = Paginator(document_rows, page_size)
    doc_page = doc_paginator.get_page(request.GET.get('page'))

    # Orphans are overwhelmingly "classified as not relevant, so no
    # TrackedEmail was ever created for it to hang off of" -- routine noise,
    # not something worth a full detail row per email. Only a non-success
    # orphan (e.g. the classifier itself errored out) reflects something a
    # human should actually look at, so those alone get the detailed table;
    # the routine ones collapse into a per-day count.
    page_one_orphans = orphans if doc_page.number == 1 else []
    orphan_needs_attention = [r for r in page_one_orphans if r.status != AgentRun.STATUS_SUCCESS]
    orphan_routine = [r for r in page_one_orphans if r.status == AgentRun.STATUS_SUCCESS]
    routine_daily_counts = Counter(timezone.localtime(r.created_at).date() for r in orphan_routine)

    context['page_obj'] = doc_page
    context['document_rows'] = doc_page.object_list
    context['orphan_runs'] = orphan_needs_attention
    context['orphan_routine_total'] = len(orphan_routine)
    context['orphan_routine_daily'] = sorted(routine_daily_counts.items(), reverse=True)
    context['truncated'] = len(recent_runs) >= settings.EMAILAGENT_ACTIVITY_MAX_ROWS
    return render(request, 'emailagent/agent_activity.html', context)


@csrf_exempt
@require_POST
def gmail_push_webhook(request):
    """Pub/Sub push endpoint -- Gmail publishes here (via the topic
    configured in GMAIL_PUBSUB_TOPIC / `manage.py watch_gmail`) whenever the
    watched mailbox changes, so new mail gets picked up immediately instead
    of waiting for the next scheduled poll_gmail run.

    Configure the Pub/Sub subscription's push endpoint as
    https://<host>/emailagent/gmail/webhook/?token=<GMAIL_PUSH_TOKEN>.

    We don't need to parse the notification's historyId -- poll_gmail()
    already does an incremental diff from our own stored watermark, so any
    notification is just a trigger to run that early. Acknowledges
    immediately and runs the sync in a background thread, since
    classification can take longer than Pub/Sub's ack deadline.
    """
    if not settings.GMAIL_PUSH_TOKEN or request.GET.get('token') != settings.GMAIL_PUSH_TOKEN:
        return HttpResponseForbidden('Invalid token')

    try:
        envelope = json.loads(request.body)
        payload = json.loads(base64.b64decode(envelope['message']['data']))
        logger.info(f"Gmail push notification received: {payload}")
    except (KeyError, ValueError) as e:
        logger.warning(f"Malformed Pub/Sub push payload: {e}")
        return HttpResponse(status=200)  # ack anyway -- retrying won't help a malformed payload

    threading.Thread(target=_run_triggered_poll, daemon=True).start()
    return HttpResponse(status=200)


def _run_triggered_poll():
    try:
        stats = services.poll_gmail()
        logger.info(
            f"Gmail push-triggered poll: processed={stats['processed']} "
            f"skipped={stats['skipped']} errors={stats['errors']}"
        )
    except Exception:
        logger.exception("Gmail push-triggered poll failed")
