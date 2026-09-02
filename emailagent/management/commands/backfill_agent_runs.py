"""
One-off backfill: reconstructs AgentRun history for TrackedEmails and
QuotationDrafts that were classified/drafted BEFORE the supervisor
(emailagent/supervisor.py) existed -- so the Agent Activity dashboard has
real examples from the existing email-tracking and quotation-drafting
history instead of being empty for everything processed earlier.

Idempotent -- for each TrackedEmail/QuotationDraft, skips it if an AgentRun
for that agent already exists (whether from a previous backfill run or a
real live run recorded since), so it's always safe to re-run.

Timestamps are set to the real historical moment (TrackedEmail.classified_at,
QuotationDraft.generated_at) rather than "now", so the Agent Activity
dashboard reads as an accurate timeline, not as if everything just happened.
duration_ms is left unset for backfilled runs -- that was never recorded at
the time, so it's left honestly unknown rather than guessed at.

Usage:
    python manage.py backfill_agent_runs --dry-run
    python manage.py backfill_agent_runs
"""
from django.core.management.base import BaseCommand

from emailagent import supervisor
from emailagent.models import AgentRun, QuotationDraft, TrackedEmail


class Command(BaseCommand):
    help = "Reconstruct AgentRun history for emails/quotations processed before the supervisor existed."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                             help='Show what would be created without saving anything.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        prefix = '[DRY RUN] ' if dry_run else ''

        already_classified = set(
            AgentRun.objects
            .filter(agent_name=AgentRun.AGENT_CLASSIFY, tracked_email__isnull=False)
            .values_list('tracked_email_id', flat=True)
        )
        already_drafted = set(
            AgentRun.objects
            .filter(agent_name=AgentRun.AGENT_DRAFT, tracked_email__isnull=False)
            .values_list('tracked_email_id', flat=True)
        )

        classify_created = 0
        draft_created = 0

        # -- Classification stage: one per TrackedEmail that was actually
        # classified (classified_at set) and doesn't already have one.
        emails = (TrackedEmail.objects
                  .exclude(classified_at__isnull=True)
                  .exclude(id__in=already_classified))
        for te in emails.iterator():
            issues = []
            if te.classification_category == 'uncertain' or te.status == TrackedEmail.STATUS_NEEDS_REVIEW:
                issues.append("Classification uncertain -- needs manual review.")
            confidence = f"{te.classification_confidence:.2f}" if te.classification_confidence is not None else "—"
            summary = f"category={te.classification_category or '—'} confidence={confidence} items={te.items.count()}"
            status = AgentRun.STATUS_FLAGGED if issues else AgentRun.STATUS_SUCCESS

            self.stdout.write(
                f"{prefix}CLASSIFY  [{status:8}] {te.subject or '(no subject)':60.60} ({te.classified_at:%Y-%m-%d %H:%M})"
            )
            if not dry_run:
                AgentRun.objects.create(
                    agent_name=AgentRun.AGENT_CLASSIFY,
                    tracked_email=te,
                    status=status,
                    summary=summary,
                    issues=issues,
                    started_at=te.classified_at,
                    finished_at=te.classified_at,
                    created_at=te.classified_at,
                )
            classify_created += 1

        # -- Drafting stage: one per QuotationDraft not already backfilled/recorded.
        # Reuses supervisor.evaluate_draft so "what counts as flagged" stays in
        # sync with what a live draft_quotation run would be judged on.
        drafts = (QuotationDraft.objects
                  .select_related('tracked_email', 'quotation', 'matched_customer')
                  .exclude(tracked_email_id__in=already_drafted))
        for draft in drafts.iterator():
            status, error, summary, issues, quotation = supervisor.evaluate_draft(draft)
            when = draft.generated_at or draft.created_at

            self.stdout.write(
                f"{prefix}DRAFT     [{status:8}] "
                f"{(draft.tracked_email.subject or '(no subject)'):60.60} ({when:%Y-%m-%d %H:%M})"
            )
            if not dry_run:
                AgentRun.objects.create(
                    agent_name=AgentRun.AGENT_DRAFT,
                    tracked_email=draft.tracked_email,
                    quotation=quotation,
                    status=status,
                    error=error,
                    summary=summary,
                    issues=issues,
                    started_at=when,
                    finished_at=when,
                    created_at=when,
                )
            draft_created += 1

        self.stdout.write("\n" + "=" * 70)
        self.stdout.write(
            f"Classification runs backfilled: {classify_created} | Draft runs backfilled: {draft_created}"
        )
        if dry_run:
            self.stdout.write("Dry run -- nothing was saved. Re-run without --dry-run to apply.")
