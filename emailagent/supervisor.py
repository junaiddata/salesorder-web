"""Supervisor: a monitoring/observability layer over the other agents in
this app (classify_email, draft_quotation, rematch_unmatched_items,
recheck_item_brand_matches, and the deterministic process_lpo -- see
emailagent/lpo_agent.py).

Records what each run took and found -- timing, success/failure, and any
issue worth a human's attention (unmatched items, a meters/length quantity
that had to be converted, a low-confidence customer match, an uncertain
classification, an LPO left needing manual review) as an AgentRun row.

This is purely observational: it never edits a TrackedEmail, EnquiryItem, or
Quotation itself -- it only ever writes its own AgentRun records, derived
from what the agent it's watching already produced. A failure to log must
never break the pipeline being observed, so every entry point here catches
and logs its own errors instead of raising.
"""
import logging

from django.utils import timezone

from .models import AgentRun

logger = logging.getLogger(__name__)


class AgentRunRecorder:
    """Bound to one in-flight agent run: start it, let the caller fill in
    what it learns (summary/issues/quotation) as it goes, then finish() to
    time and persist it."""

    def __init__(self, agent_name, tracked_email=None, quotation=None, submittal=None, sales_order=None):
        self.run = AgentRun(
            agent_name=agent_name,
            tracked_email=tracked_email,
            quotation=quotation,
            submittal=submittal,
            sales_order=sales_order,
            started_at=timezone.now(),
        )

    def fail(self, error):
        self.run.status = AgentRun.STATUS_FAILED
        self.run.error = str(error)[:2000]

    def finish(self, summary='', issues=None, quotation=None, submittal=None, sales_order=None):
        if self.run.status != AgentRun.STATUS_FAILED:
            self.run.status = AgentRun.STATUS_FLAGGED if issues else AgentRun.STATUS_SUCCESS
        if summary:
            self.run.summary = summary[:500]
        if issues:
            self.run.issues = issues
        if quotation is not None:
            self.run.quotation = quotation
        if submittal is not None:
            self.run.submittal = submittal
        if sales_order is not None:
            self.run.sales_order = sales_order
        self.run.finished_at = timezone.now()
        self.run.duration_ms = int((self.run.finished_at - self.run.started_at).total_seconds() * 1000)
        try:
            self.run.save()
        except Exception:
            logger.exception("Failed to persist AgentRun (monitoring only -- not raised further)")


def evaluate_draft(draft):
    """Pure, read-only inspection of a QuotationDraft -- derives what an
    AgentRun for it should look like (status, error, summary, issues, the
    linked quotation) without creating or modifying anything. Shared by the
    live path (finalize_draft_run, called right after draft_quotation runs)
    and by the one-off backfill_agent_runs command, which reconstructs
    history for drafts that predate the supervisor -- keeping both in
    agreement about what counts as "flagged" for a draft."""
    from .models import QuotationDraft

    # Exact, per-line reason for every requirement item the agent could NOT
    # put on the quotation -- read straight from EnquiryItem.match_notes
    # (set by draft_quotation()/rematch/recheck) rather than re-derived from
    # the quotation's remarks text, so this stays accurate even if that
    # wording changes and still works when no quotation was created at all.
    # Shared with the quotations list (so/templates) via the same model
    # method, so both places show identical reasons.
    item_issues = draft.unmatched_item_issues()

    if draft.status == QuotationDraft.STATUS_FAILED:
        error = draft.error or "Drafting failed with no recorded error."
        return AgentRun.STATUS_FAILED, error, '', item_issues, draft.quotation

    if draft.status == QuotationDraft.STATUS_MERGED:
        # A same-thread follow-up merged into an earlier email's quotation
        # instead of creating its own -- see quotation_agent.merge_followup_into_quotation.
        who = draft.merged_into.quotation_number if draft.merged_into_id else '—'
        summary = f"merged into quotation={who}"
        return AgentRun.STATUS_FLAGGED, '', summary, item_issues, draft.merged_into

    quotation = draft.quotation
    remarks = (quotation.remarks or '') if quotation else ''
    issues = []
    if draft.is_empty_quotation():
        issues.append(
            "Quotation has ZERO items -- none of the requirement items could be "
            "auto-matched, so nothing was actually quoted. Needs manual completion "
            "before it should be approved."
        )
    issues.extend(item_issues)
    if 'Unit/quantity conversions applied' in remarks:
        issues.append("A meters/length quantity was converted to pieces/rolls -- verify before approving.")
    if not draft.matched_customer_id:
        issues.append(f"No confident customer match -- quoted as walk-in ({draft.customer_guess or 'unnamed'}).")

    who = draft.matched_customer.customer_name if draft.matched_customer_id else (draft.customer_guess or 'walk-in')
    summary = f"quotation={quotation.quotation_number if quotation else '—'} | customer={who}"
    status = AgentRun.STATUS_FLAGGED if issues else AgentRun.STATUS_SUCCESS
    return status, '', summary, issues, quotation


def finalize_draft_run(recorder, tracked_email):
    """Inspects the QuotationDraft/Quotation draft_quotation() just produced
    for `tracked_email` and finishes `recorder` accordingly -- read-only,
    never modifies either. Safe to call even if draft_quotation raised
    something unexpected (falls back to a generic failure note)."""
    try:
        from .models import QuotationDraft

        draft = (QuotationDraft.objects
                 .filter(tracked_email=tracked_email)
                 .select_related('quotation', 'matched_customer')
                 .first())
        if not draft:
            recorder.fail("draft_quotation completed but left no QuotationDraft row for this email.")
            recorder.finish()
            return

        status, error, summary, issues, quotation = evaluate_draft(draft)
        if status == AgentRun.STATUS_FAILED:
            recorder.fail(error)
            recorder.finish(issues=issues, quotation=quotation)
        else:
            recorder.finish(summary=summary, issues=issues, quotation=quotation)
    except Exception as exc:
        logger.exception("finalize_draft_run itself failed (monitoring only)")
        recorder.fail(f"Supervisor could not evaluate the draft: {exc}")
        recorder.finish()


def evaluate_lpo(lpo_request):
    """Pure, read-only inspection of an LPORequest -- derives what an
    AgentRun for it should look like (status, error, summary, issues, the
    matched quotation, the created sales order), mirroring evaluate_draft's
    role for QuotationDraft. NEEDS_REVIEW is always FLAGGED (a human has to
    act, even though lpo_agent itself didn't error) -- it's never treated
    as a plain success, since leaving an LPO unmatched/unconverted is
    exactly the kind of thing this dashboard exists to surface."""
    from .models import LPORequest

    if lpo_request.status == LPORequest.STATUS_FAILED:
        error = lpo_request.error or "LPO processing failed with no recorded error."
        return AgentRun.STATUS_FAILED, error, '', [], lpo_request.matched_quotation, None

    if lpo_request.status == LPORequest.STATUS_NEEDS_REVIEW:
        issues = [lpo_request.match_reasoning] if lpo_request.match_reasoning else []
        summary = f"lpo={lpo_request.lpo_number or '—'} -- needs review ({lpo_request.get_match_method_display()})"
        return AgentRun.STATUS_FLAGGED, '', summary, issues, lpo_request.matched_quotation, None

    if lpo_request.status == LPORequest.STATUS_CONFIRMED:
        quotation_number = lpo_request.matched_quotation.quotation_number if lpo_request.matched_quotation_id else '—'
        order_number = lpo_request.sales_order.order_number if lpo_request.sales_order_id else '—'
        # Sourced from the SalesOrder itself (see so.models.SalesOrder.created_via)
        # rather than inferred from how this AgentRun was triggered, so the label is
        # correct however/whenever confirmation actually happened -- immediately at
        # ingestion (agent_lpo), a later lpo_request_recheck_match (also agent_lpo),
        # or a human on this review page (manual).
        via = lpo_request.sales_order.created_via if lpo_request.sales_order_id else ''
        via_label = ' (auto)' if via == 'agent_lpo' else ' (manual)' if via == 'manual' else ''
        summary = f"lpo={lpo_request.lpo_number or '—'} matched={quotation_number} sales_order={order_number}{via_label}"
        return AgentRun.STATUS_SUCCESS, '', summary, [], lpo_request.matched_quotation, lpo_request.sales_order

    # PENDING/DISMISSED shouldn't reach here (finalize_lpo_run only runs
    # right after process_lpo, which always leaves NEEDS_REVIEW/CONFIRMED/
    # FAILED) -- fall back to a generic flagged note rather than assuming.
    return AgentRun.STATUS_FLAGGED, '', f"lpo={lpo_request.lpo_number or '—'} -- status={lpo_request.status}", [], lpo_request.matched_quotation, lpo_request.sales_order


def finalize_lpo_run(recorder, tracked_email):
    """Inspects the LPORequest lpo_agent.process_lpo() just produced for
    `tracked_email` and finishes `recorder` accordingly -- read-only, never
    modifies either. Safe to call even if process_lpo raised something
    unexpected (falls back to a generic failure note)."""
    try:
        from .models import LPORequest

        lpo_request = (LPORequest.objects
                       .filter(tracked_email=tracked_email)
                       .select_related('matched_quotation', 'sales_order')
                       .first())
        if not lpo_request:
            recorder.fail("process_lpo completed but left no LPORequest row for this email.")
            recorder.finish()
            return

        status, error, summary, issues, quotation, sales_order = evaluate_lpo(lpo_request)
        if status == AgentRun.STATUS_FAILED:
            recorder.fail(error)
            recorder.finish(issues=issues, quotation=quotation, sales_order=sales_order)
        else:
            recorder.finish(summary=summary, issues=issues, quotation=quotation, sales_order=sales_order)
    except Exception as exc:
        logger.exception("finalize_lpo_run itself failed (monitoring only)")
        recorder.fail(f"Supervisor could not evaluate the LPO request: {exc}")
        recorder.finish()
