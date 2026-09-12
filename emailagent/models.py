from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


def email_attachment_path(instance, filename):
    # Keyed on the TrackedEmail's own PK, not gmail_message_id -- Gmail's
    # message ids are short/filesystem-safe, but an Outlook/IMAP message id
    # is the raw RFC822 Message-ID header (e.g.
    # "<001e01dd35f5$c66fd3a0$534f7ae0$@proton.ae>"), which contains
    # characters Windows rejects in folder names (<, >, $, @, :).
    return f'email_attachments/{instance.tracked_email_id}/{filename}'


class TrackedEmail(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_RFQ = 'rfq'
    STATUS_SUBMITTAL = 'submittal'
    STATUS_LPO = 'lpo'
    STATUS_NOT_RELEVANT = 'not_relevant'
    STATUS_NEEDS_REVIEW = 'needs_review'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending Classification'),
        (STATUS_RFQ, 'RFQ / Enquiry'),
        (STATUS_SUBMITTAL, 'Submittal Request'),
        (STATUS_LPO, 'Purchase Order (LPO)'),
        (STATUS_NOT_RELEVANT, 'Not Relevant'),
        (STATUS_NEEDS_REVIEW, 'Needs Manual Review'),
    ]

    SOURCE_GMAIL = 'gmail'
    SOURCE_OUTLOOK = 'outlook'
    # Third IMAP mailbox (project@junaid.ae) -- classified normally like any
    # other source, and a genuine RFQ here DOES get a quotation drafted; only
    # LPO/Sales-Order processing is always skipped for it (see
    # emailagent.services.process_new_message's allow_quotation/allow_lpo
    # params, and poll_project_mailbox). Kept as its own `source` value, not
    # a flag on SOURCE_OUTLOOK, so this mailbox's messages stay clearly
    # distinguishable from the primary sales@junaid.ae mailbox everywhere
    # `source` is already shown/filtered.
    SOURCE_PROJECT = 'project'
    # Fourth IMAP mailbox -- fully submittal-only, unlike SOURCE_PROJECT
    # above: neither quotation drafting nor LPO processing ever runs for it
    # (see poll_submittal_mailbox / process_new_message's allow_quotation/
    # allow_lpo params). Kept as its own source value for the same reason
    # SOURCE_PROJECT is: distinguishable everywhere `source` is shown/filtered.
    SOURCE_SUBMITTAL = 'submittal'
    SOURCE_CHOICES = [
        (SOURCE_GMAIL, 'Gmail'),
        (SOURCE_OUTLOOK, 'Outlook'),
        (SOURCE_PROJECT, 'Project Mailbox'),
        (SOURCE_SUBMITTAL, 'Submittal Mailbox'),
    ]

    # Holds the provider's own Message-ID for either source (Gmail API's
    # message id, or the RFC822 Message-ID header for IMAP/Outlook) --
    # widened past Gmail's own ~20-char ids to fit longer Message-ID headers.
    gmail_message_id = models.CharField(max_length=255, unique=True, db_index=True)
    thread_id = models.CharField(max_length=255, db_index=True, blank=True, default='')
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=SOURCE_GMAIL, db_index=True)
    # IMAP UID for the three IMAP-sourced mailboxes (outlook/project/submittal)
    # -- blank for Gmail. On this mail host (Zimbra), the IMAP UID doubles as
    # Zimbra's own internal item id. Not currently used by views.open_webmail
    # (which just lands on the mailbox's Inbox rather than deep-linking to
    # one message), but kept as a stable per-message identifier on this host
    # for any future use. Never populated retroactively -- blank on any
    # email tracked before this field existed.
    imap_uid = models.CharField(max_length=32, blank=True, default='')

    sender = models.CharField(max_length=320)
    sender_name = models.CharField(max_length=255, blank=True, default='')
    # Bcc is almost always empty for received mail -- email transport doesn't
    # deliver Bcc headers to recipients other than the sender's own copy.
    to_recipients = models.JSONField(default=list, blank=True)
    cc_recipients = models.JSONField(default=list, blank=True)
    bcc_recipients = models.JSONField(default=list, blank=True)

    subject = models.CharField(max_length=998, blank=True, default='')
    body_text = models.TextField(blank=True, default='')
    body_html = models.TextField(blank=True, default='')

    received_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    raw_headers = models.JSONField(default=list, blank=True)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    classification_category = models.CharField(max_length=20, blank=True, default='')
    classification_confidence = models.FloatField(null=True, blank=True)
    classification_reasoning = models.TextField(blank=True, default='')
    classified_at = models.DateTimeField(null=True, blank=True)
    classification_model = models.CharField(max_length=60, blank=True, default='')

    # Captured from the classifier at intake time (see classifier.py's
    # submittal_project/submittal_client/etc.) even though submittal
    # drafting no longer happens automatically -- a human may pick which
    # submittal_request_items to draft well after classification, so these
    # need to survive independently of that one-time classification result
    # (see submittal_agent.draft_submittals_for_selected_items).
    submittal_brand = models.CharField(max_length=255, blank=True, default='')
    submittal_project = models.TextField(blank=True, default='')
    submittal_client = models.CharField(max_length=255, blank=True, default='')
    submittal_consultant = models.CharField(max_length=255, blank=True, default='')
    submittal_main_contractor = models.CharField(max_length=255, blank=True, default='')
    submittal_mep_contractor = models.CharField(max_length=255, blank=True, default='')

    confirmed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='emailagent_confirmed_emails',
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-received_at']

    def __str__(self):
        return f"{self.subject or '(no subject)'} — {self.sender}"

    def categories(self):
        """Distinct product categories across this enquiry's items -- both
        RFQ/pricing items (EnquiryItem) and, for a submittal-only or
        combined email, requested submittal products (SubmittalRequestItem)
        -- so the All Emails list's Categories column reflects a
        submittal-only email too, not just a priced RFQ. Uses the
        prefetched/cached items/submittal_request_items querysets when
        available (no extra query)."""
        seen = []
        for item in self.items.all():
            if item.category and item.category not in seen:
                seen.append(item.category)
        for item in self.submittal_request_items.all():
            if item.category and item.category not in seen:
                seen.append(item.category)
        return seen

    def extraction_issues(self):
        """Human-readable reasons this email's extracted item list might be
        incomplete or wrong -- e.g. an attachment that was never actually
        read, or an RFQ with no items at all -- so a human scanning the
        email list can immediately see WHY, instead of having to open the
        email to guess. Uses the prefetched/cached attachments/items
        querysets when available (no extra query)."""
        from django.conf import settings

        issues = []
        for att in self.attachments.all():
            if att.included_in_classification:
                continue
            max_mb = settings.EMAILAGENT_MAX_ATTACHMENT_MB
            size_mb = (att.size_bytes or 0) / (1024 * 1024)
            if size_mb > max_mb:
                reason = f"exceeds the {max_mb}MB attachment limit ({size_mb:.1f}MB)"
            else:
                reason = "failed to download from Gmail"
            issues.append(
                f"Attachment \"{att.filename or 'unnamed'}\" was not read ({reason}) -- "
                "may contain items that were missed."
            )

        if self.status == self.STATUS_RFQ and not self.items.exists():
            issues.append("No product/item details were extracted from the email body or its attachments.")

        if self.status == self.STATUS_NEEDS_REVIEW and self.classification_reasoning:
            issues.append(f"Needs review: {self.classification_reasoning}")

        return issues


class EmailAttachment(models.Model):
    tracked_email = models.ForeignKey(TrackedEmail, on_delete=models.CASCADE, related_name='attachments')
    gmail_attachment_id = models.TextField(blank=True, default='')
    filename = models.CharField(max_length=255, blank=True, default='')
    content_type = models.CharField(max_length=120, blank=True, default='')
    size_bytes = models.PositiveIntegerField(default=0)

    file = models.FileField(upload_to=email_attachment_path, blank=True, null=True)

    extracted_text = models.TextField(blank=True, default='')
    included_in_classification = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return self.filename or f'attachment {self.pk}'

    def is_image(self):
        return self.content_type.startswith('image/')

    def is_pdf(self):
        return self.content_type == 'application/pdf'

    def is_excel(self):
        return self.content_type in (
            'application/vnd.ms-excel',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )


class EnquiryItem(models.Model):
    """One requirement/BOQ line item extracted from an enquiry email or its
    attachments (PDF, Excel, or a table embedded in an image)."""
    tracked_email = models.ForeignKey(TrackedEmail, on_delete=models.CASCADE, related_name='items')
    description = models.TextField()
    category = models.CharField(max_length=100, blank=True, default='', db_index=True)
    brand = models.CharField(max_length=255, blank=True, default='')
    quantity = models.CharField(max_length=50, blank=True, default='')
    unit = models.CharField(max_length=50, blank=True, default='')
    notes = models.CharField(
        max_length=255, blank=True, default='',
        help_text="e.g. 'revised from 3 to 2 on 25 Jul' -- set when the thread shows the "
                  "client changing a previously requested value, so the reason isn't hidden.",
    )
    source_attachment = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Which attachment (filename) or part of the email this item came from -- set by "
                  "the classifier when the RFQ has more than one client_requirement attachment "
                  "(e.g. two separate BOQs for two different buildings). Blank when there's only one "
                  "requirement source (the common case) or the item came from the email body. Used by "
                  "draft_quotation() to draft a SEPARATE quotation per distinct attachment instead of "
                  "merging unrelated scopes into one -- see quotation_agent._group_items_by_scope.",
    )
    order = models.PositiveIntegerField(default=0)

    # Populated by the quotation-drafting agent (quotation_agent.py) -- its
    # best-effort match of this raw requirement line against the real item
    # catalog, for a human to confirm/correct in the quotation review screen.
    matched_item = models.ForeignKey(
        'so.Items', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text="Agent's best catalog match for this requirement line, if any.",
    )
    matched_price = models.FloatField(null=True, blank=True)
    matched_unit = models.CharField(
        max_length=20, blank=True, default='pcs',
        help_text="Normalized to the quotation form's unit choices (pcs/ctn/roll) -- "
                  "`unit` above keeps the original text as extracted from the email.",
    )
    matched_quantity = models.IntegerField(
        null=True, blank=True,
        help_text="Actual quantity quoted, in matched_unit's units -- may differ from `quantity` above "
                  "when the customer's requested unit was a length measure (e.g. METERS) that had to be "
                  "converted to whole pieces/rolls against the catalog item's per-unit length.",
    )
    match_notes = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Agent's note on the match, e.g. 'no confident catalog match' or "
                  "'brand differs from what was requested'.",
    )

    # Near-miss substitutes for this line live in EnquiryItemSuggestion below
    # (related_name='suggestions') rather than in a field here, because one
    # requirement commonly has SEVERAL plausible stand-ins and picking between
    # them is the reviewer's call, not the agent's -- a 125mm clamp we don't
    # carry sits between the 4" and the 6" we do, and which one is acceptable
    # depends on the job.

    submittal = models.ForeignKey(
        'submittal.Submittal', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text="No longer set by any current code path -- submittal drafting from a Requirement "
                  "Item only happened via the (now removed) 'Generate Submittal' action on this table; "
                  "for a pure submittal-request email, use the Requested Submittal Items table instead "
                  "(see SubmittalRequestItem.submittal / submittal_agent.draft_submittals_for_selected_items). "
                  "Kept for any historical tagging from when that flow existed.",
    )

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return self.description[:60]


class EnquiryItemSuggestion(models.Model):
    """One catalog item the quotation agent found that is CLOSE to a
    requirement line but not close enough to quote on its own authority -- the
    right pipe in the wrong colour, a different fixed length, the next size up
    or down. A line can have several: "Clamp 125mm" has no exact match, and
    the 4" (113-118mm) and 6" (168-172mm) we stock are both plausible, so both
    are offered and the REVIEWER decides which (if either) is acceptable.

    Nothing here is on any quotation. The parent line stays unmatched and
    keeps showing under "needs attention" until a reviewer accepts one of
    these from the quotation screen -- see
    so/views_quotation.py::_add_suggested_item (action="add_suggested_item"),
    which is the only code path that turns a suggestion into a quoted line.

    Deliberately real rows with a real FK, rather than letting the agent name
    the codes in match_notes prose: recorded this way each code is validated
    against the catalog at draft time, so a button can only ever offer an item
    that genuinely exists, and nothing downstream has to guess which number in
    a sentence was meant to be an item code."""
    enquiry_item = models.ForeignKey(
        EnquiryItem, on_delete=models.CASCADE, related_name='suggestions',
    )
    item = models.ForeignKey(
        'so.Items', on_delete=models.CASCADE, related_name='+',
        help_text="The catalog item being offered as a substitute.",
    )
    reason = models.CharField(
        max_length=255, blank=True, default='',
        help_text="How THIS item differs from what the customer asked for, in the agent's own "
                  "words (e.g. '4\" (113-118mm) -- one size under the 125mm requested') -- shown "
                  "beside its Add button so a reviewer sees what they are accepting. Each "
                  "suggestion carries its own, since that is what distinguishes them.",
    )
    order = models.PositiveIntegerField(
        default=0,
        help_text="The agent's own ranking, best first -- suggestions are shown in this order.",
    )

    class Meta:
        ordering = ['order', 'id']
        constraints = [
            # The same catalog item twice on one line is never two options to
            # choose between, only a duplicated row for the reviewer to read.
            models.UniqueConstraint(
                fields=['enquiry_item', 'item'], name='unique_suggestion_per_enquiry_item',
            ),
        ]

    def __str__(self):
        return f"{self.item_id} suggested for enquiry item {self.enquiry_item_id}"


class SubmittalRequestItem(models.Model):
    """One requested product/model extracted from an email that asked for
    material submittal/technical-approval documents (see classifier.py's
    submittal_items, each carrying its own description/brand/category).

    Deliberately a SEPARATE model from EnquiryItem rather than reusing it:
    quotation_agent.draft_quotation() reads tracked_email.items.all() (i.e.
    EnquiryItem) to build a REAL priced quotation for every item found there
    -- mixing submittal-only products into that list would make the agent
    try to price things the customer only asked to get APPROVED, not
    quoted (a combined RFQ+submittal email can legitimately ask for pricing
    on some items and submittal approval on different ones). This model
    exists purely for tracking/display -- see TrackedEmail.categories() --
    and to feed submittal_agent.py's brand+category grouping; matching
    against the submittal materials library itself happens transiently in
    submittal_agent.py, not through this model."""
    tracked_email = models.ForeignKey(TrackedEmail, on_delete=models.CASCADE, related_name='submittal_request_items')
    description = models.TextField()
    category = models.CharField(max_length=100, blank=True, default='', db_index=True)
    brand = models.CharField(max_length=255, blank=True, default='')
    order = models.PositiveIntegerField(default=0)

    submittal = models.ForeignKey(
        'submittal.Submittal', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text="Set once a human has selected this item (checkboxes on the email page's Requested "
                  "Submittal Items table) and a submittal was drafted for it -- see "
                  "submittal_agent.draft_submittals_for_selected_items -- excludes it from being "
                  "offered for selection again.",
    )

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return self.description[:60]


class QuotationDraft(models.Model):
    """One per RFQ TrackedEmail -- tracks the quotation-drafting agent's
    progress (item/customer matching) and the real so.Quotation it
    automatically created (a person can still edit that quotation
    normally -- this just records where it came from)."""
    STATUS_PENDING = 'pending'
    STATUS_READY = 'ready'
    STATUS_CONFIRMED = 'confirmed'
    STATUS_FAILED = 'failed'
    STATUS_MERGED = 'merged'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Drafting'),
        (STATUS_READY, 'Ready for Review'),
        (STATUS_CONFIRMED, 'Quotation Created'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_MERGED, 'Merged into earlier quotation'),
    ]

    tracked_email = models.OneToOneField(TrackedEmail, on_delete=models.CASCADE, related_name='quotation_draft')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)

    matched_customer = models.ForeignKey(
        'so.Customer', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text="Agent's best match against existing customers, if any.",
    )
    customer_guess = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Best-guess display name, used when there is no confident customer match "
                  "(quoted as a walk-in/CASH customer under this name).",
    )
    reasoning = models.TextField(blank=True, default='')
    error = models.TextField(blank=True, default='')
    generated_at = models.DateTimeField(null=True, blank=True)

    quotation = models.OneToOneField(
        'so.Quotation', on_delete=models.SET_NULL, null=True, blank=True, related_name='quotation_draft',
        help_text="The quotation this draft was automatically turned into.",
    )
    merged_into = models.ForeignKey(
        'so.Quotation', on_delete=models.SET_NULL, null=True, blank=True, related_name='merged_drafts',
        help_text="Set (instead of `quotation`) when this email was a same-thread follow-up whose "
                  "changes were merged into an earlier email's quotation rather than creating a "
                  "second one for the same enquiry.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Quotation draft for {self.tracked_email_id} ({self.status})"

    def unmatched_item_issues(self, exclude_suggested=False):
        """One line per requirement item that could NOT be put on the
        quotation, with its exact reason (from EnquiryItem.match_notes) --
        e.g. no catalog match found, or a match was found but is out of
        stock. Shared by the Agent Activity dashboard (supervisor.py), the
        quotations list, and the quotation drafts queue, so the same exact
        reasons show up everywhere instead of a vague "some items didn't
        match". Filters in Python (over .all()) rather than issuing its own
        .filter() query, so a caller that prefetched tracked_email__items
        gets the benefit -- one that didn't still works, just less
        efficiently.

        `exclude_suggested` drops the lines that have substitutes to offer
        (see unmatched_item_suggestions) -- the quotation screen renders those
        as their own block naming the same requirement, so listing them here
        too says everything twice. The dashboards leave it False and still see
        every unquoted line.
        """
        if not self.tracked_email_id:
            return []
        return [
            f"Not quoted -- \"{item.description[:80]}\": {item.match_notes or 'no reason recorded.'}"
            for item in self.tracked_email.items.all()
            if not item.matched_item_id
            and not (exclude_suggested and item.suggestions.all())
        ]

    def unmatched_item_suggestions(self):
        """The subset of the unquoted requirement lines above that the agent
        found at least one near-miss substitute for (EnquiryItemSuggestion) --
        the quotation screen renders each line with one "Add to quotation"
        button per suggestion, see
        so/views_quotation.py::_add_suggested_item.

        Returns the EnquiryItems themselves (not strings like
        unmatched_item_issues above) so the template can show each suggested
        item's real description, price and stock -- a reviewer choosing
        between substitutes has to be able to see what they are choosing
        between. Kept as a separate method rather than changing
        unmatched_item_issues' return type, which the Agent Activity dashboard
        and the drafts queue both rely on being a plain list of strings."""
        if not self.tracked_email_id:
            return []
        return [
            item for item in self.tracked_email.items.all()
            if not item.matched_item_id and item.suggestions.all()
        ]

    def is_empty_quotation(self):
        """True if this draft's quotation exists but has no line items --
        nothing could be auto-matched, so there's nothing to actually quote
        yet. Uses .all() (not .exists()) so a prefetched quotation__items
        cache is reused instead of issuing a fresh query."""
        return bool(self.quotation_id) and not self.quotation.items.all()


class AdditionalQuotationDraft(models.Model):
    """When one RFQ email covers MULTIPLE distinct requirement scopes --
    e.g. two separate BOQ attachments for two different buildings/projects
    (see EnquiryItem.source_attachment) -- draft_quotation() drafts the
    FIRST scope as the email's normal, PRIMARY QuotationDraft exactly as
    always, and one of these (plus one more real so.Quotation) for each
    ADDITIONAL scope beyond the first.

    Deliberately a separate model rather than allowing more than one
    QuotationDraft per email: QuotationDraft.tracked_email is a OneToOne
    relied on throughout the pipeline (merge-follow-up detection, Agent
    Activity grouping, the quotations-list "Agent" badge/source filter via
    Quotation.quotation_draft, etc.) as "the one draft for this email" --
    changing that to a one-to-many relation would have meant auditing every
    one of those call sites. Keeping the extra scopes here instead means
    all of that existing, working behavior for the PRIMARY draft/quotation
    needs zero changes; this model only adds new, purely additive
    observability (its own reverse accessor, see Quotation.additional_quotation_draft)
    for the sibling quotations."""
    tracked_email = models.ForeignKey(
        TrackedEmail, on_delete=models.CASCADE, related_name='additional_quotation_drafts',
    )
    source_attachment = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Which attachment/scope this quotation was drafted from (matches "
                  "EnquiryItem.source_attachment for the items on it).",
    )
    quotation = models.OneToOneField(
        'so.Quotation', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='additional_quotation_draft',
        help_text="The quotation this additional scope was automatically turned into.",
    )
    reasoning = models.TextField(blank=True, default='')
    error = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        return f"Additional quotation draft ({self.source_attachment or 'untitled scope'}) for email {self.tracked_email_id}"

    def unmatched_item_issues(self, exclude_suggested=False):
        """Same idea as QuotationDraft.unmatched_item_issues, but scoped to
        just THIS scope's items (by source_attachment) rather than the
        whole email -- a multi-scope enquiry drafts a separate quotation
        per scope, so each one's issues list should only cover its own
        items, not a sibling scope's."""
        return [
            f"Not quoted -- \"{item.description[:80]}\": {item.match_notes or 'no reason recorded.'}"
            for item in self.tracked_email.items.filter(source_attachment=self.source_attachment)
            if not item.matched_item_id
            and not (exclude_suggested and item.suggestions.all())
        ]

    def unmatched_item_suggestions(self):
        """Same as QuotationDraft.unmatched_item_suggestions, scoped to just
        THIS scope's items -- see unmatched_item_issues above for why the
        scoping matters."""
        return [
            item for item in self.tracked_email.items.filter(source_attachment=self.source_attachment)
            if not item.matched_item_id and item.suggestions.all()
        ]


class SubmittalDraft(models.Model):
    """One per submittal auto-drafted by the submittal agent (submittal_agent.py)
    -- either from a same-thread email that asked for material submittal/
    technical approval documents (tracked_email set), or from an existing
    Quotation's line items via the 'Generate Submittal' action on the
    quotation page (source_quotation set instead). Mirrors QuotationDraft's
    role: tracks the agent's own matching/reasoning, while the real
    submittal.Submittal it created is a normal record a person edits through
    the regular submittal wizard. Always created with
    Submittal.status=STATUS_NEEDS_REVIEW -- a human must open and verify it
    (see Submittal.mark_verified) before it can be emailed to a client."""
    STATUS_PENDING = 'pending'
    STATUS_READY = 'ready'
    STATUS_CONFIRMED = 'confirmed'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Drafting'),
        (STATUS_READY, 'Ready for Review'),
        (STATUS_CONFIRMED, 'Submittal Created'),
        (STATUS_FAILED, 'Failed'),
    ]

    tracked_email = models.OneToOneField(
        TrackedEmail, on_delete=models.CASCADE, null=True, blank=True, related_name='submittal_draft',
        help_text="The email that asked for a material submittal, if this draft came from an email "
                  "rather than an existing quotation.",
    )
    source_quotation = models.ForeignKey(
        'so.Quotation', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text="The quotation this draft was generated from, if triggered via 'Generate Submittal' "
                  "rather than an email.",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)

    matched_brand = models.ForeignKey(
        'submittal.SubmittalBrand', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text="Agent's best match for which catalog brand this submittal is for.",
    )
    category = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Product category this submittal package covers, if the email's requested items "
                  "carried one (see classifier.py's submittal_items and "
                  "AdditionalSubmittalDraft.category, which this mirrors for the PRIMARY group).",
    )
    reasoning = models.TextField(blank=True, default='')
    error = models.TextField(blank=True, default='')
    generated_at = models.DateTimeField(null=True, blank=True)

    submittal = models.OneToOneField(
        'submittal.Submittal', on_delete=models.SET_NULL, null=True, blank=True, related_name='draft_record',
        help_text="The submittal this draft was automatically turned into.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        source = f"email {self.tracked_email_id}" if self.tracked_email_id else f"quotation {self.source_quotation_id}"
        return f"Submittal draft from {source} ({self.status})"


class AdditionalSubmittalDraft(models.Model):
    """When one email requests material submittals covering MULTIPLE distinct
    brand+category combinations (see classifier.py's submittal_items, each
    carrying its own brand/category) -- draft_submittal_from_email drafts the
    FIRST combination as the email's normal, PRIMARY SubmittalDraft exactly
    as always, and one of these (plus one more real submittal.Submittal) for
    each ADDITIONAL combination beyond the first.

    Mirrors AdditionalQuotationDraft's role/reasoning: kept as a separate
    model rather than allowing more than one SubmittalDraft per email, since
    SubmittalDraft.tracked_email is a OneToOne relied on as "the one draft
    for this email" (email_detail's action buttons, the Submittal Drafts
    queue, etc.) -- this model only adds new, purely additive observability
    for the sibling submittals."""
    tracked_email = models.ForeignKey(
        TrackedEmail, on_delete=models.CASCADE, related_name='additional_submittal_drafts',
    )
    brand = models.CharField(max_length=255, blank=True, default='')
    category = models.CharField(max_length=255, blank=True, default='')
    submittal = models.OneToOneField(
        'submittal.Submittal', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='additional_submittal_draft',
        help_text="The submittal this additional brand/category combination was automatically turned into.",
    )
    reasoning = models.TextField(blank=True, default='')
    error = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']

    def __str__(self):
        label = ' / '.join(p for p in (self.brand, self.category) if p) or 'untitled group'
        return f"Additional submittal draft ({label}) for email {self.tracked_email_id}"


class LPORequest(models.Model):
    """One per email carrying a client's own Purchase Order / LPO PDF (see
    classifier.py's is_lpo/lpo_* fields) -- tracks the extracted details and
    the emailagent.lpo_agent matching/auto-creation outcome. Mirrors
    QuotationDraft's role: a OneToOne with TrackedEmail, extracted data kept
    here for audit even when nothing could be auto-matched, and (when
    matching succeeds confidently -- see lpo_agent.find_matching_quotation)
    the real so.SalesOrder it produced. v1 supports exactly ONE LPO per
    email, same simplification is_submittal_request originally had before
    AdditionalSubmittalDraft existed -- a second PO in the same email is not
    yet captured."""
    STATUS_PENDING = 'pending'
    STATUS_NEEDS_REVIEW = 'needs_review'
    STATUS_CONFIRMED = 'confirmed'
    STATUS_DISMISSED = 'dismissed'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Processing'),
        (STATUS_NEEDS_REVIEW, 'Needs Review'),
        (STATUS_CONFIRMED, 'Sales Order Created'),
        (STATUS_DISMISSED, 'Dismissed'),
        (STATUS_FAILED, 'Failed'),
    ]

    MATCH_NONE = 'none'
    MATCH_EXACT_NUMBER = 'exact_number'
    MATCH_FUZZY_NUMBER = 'fuzzy_number'
    MATCH_CUSTOMER_ITEM = 'customer_item_overlap'
    MATCH_METHOD_CHOICES = [
        (MATCH_NONE, 'No match found'),
        (MATCH_EXACT_NUMBER, 'Exact quotation number match'),
        (MATCH_FUZZY_NUMBER, 'Fuzzy quotation number match'),
        (MATCH_CUSTOMER_ITEM, 'Customer + item overlap match'),
    ]

    tracked_email = models.OneToOneField(TrackedEmail, on_delete=models.CASCADE, related_name='lpo_request')

    # Extracted verbatim from the classifier -- see classifier.py's lpo_*
    # submit_classification args. Kept as free text (not typed/parsed)
    # except total_amount, same reasoning as EnquiryItem.quantity staying
    # text: LLM-extracted currency/date formatting is unreliable to force
    # into a stricter field at this boundary.
    lpo_number = models.CharField(max_length=100, blank=True, default='')
    lpo_date = models.CharField(max_length=100, blank=True, default='')
    customer_name_stated = models.CharField(max_length=255, blank=True, default='')
    referenced_quotation_number = models.CharField(max_length=100, blank=True, default='')
    delivery_terms = models.TextField(blank=True, default='')
    payment_terms = models.TextField(blank=True, default='')
    total_amount = models.FloatField(
        null=True, blank=True,
        help_text="Grand total / total incl. VAT, parsed from the classifier's lpo_total_amount text "
                  "(currency symbols/commas stripped) -- null if it couldn't be parsed as a number.",
    )
    total_discount = models.FloatField(
        null=True, blank=True,
        help_text="Total discount amount stated on the LPO, if broken out as its own totals-block line.",
    )
    total_excl_vat = models.FloatField(
        null=True, blank=True,
        help_text="Total amount excl. VAT stated on the LPO, if broken out as its own totals-block line.",
    )
    total_vat = models.FloatField(
        null=True, blank=True,
        help_text="Total VAT amount stated on the LPO, if broken out as its own totals-block line.",
    )
    amount_in_words = models.CharField(
        max_length=255, blank=True, default='',
        help_text="The grand total spelled out in words, exactly as stated on the LPO (e.g. 'Seven "
                  "thousand five hundred six and 35/100 AED ONLY'). '' if not stated.",
    )

    source_attachment = models.ForeignKey(
        EmailAttachment, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text="The attachment the classifier tagged source='lpo_document' -- the actual PO PDF, "
                  "re-served via the existing emailagent:attachment_download view rather than duplicated.",
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)

    matched_quotation = models.ForeignKey(
        'so.Quotation', on_delete=models.SET_NULL, null=True, blank=True, related_name='lpo_requests',
        help_text="The quotation this LPO was matched to, if any -- set for every match_method except 'none'.",
    )
    match_method = models.CharField(max_length=25, choices=MATCH_METHOD_CHOICES, default=MATCH_NONE)
    match_score = models.FloatField(
        null=True, blank=True,
        help_text="Informational only -- see lpo_agent.find_matching_quotation's confidence policy. Never "
                  "gates auto-creation by itself; only an exact, unique quotation-number match does.",
    )
    candidate_quotations = models.ManyToManyField(
        'so.Quotation', blank=True, related_name='+',
        help_text="Every plausible quotation surfaced for a human to pick from on the review page when "
                  "match_method is fuzzy_number/customer_item_overlap -- never populated for exact_number, "
                  "since that path is unambiguous by construction.",
    )
    match_reasoning = models.TextField(blank=True, default='')

    sales_order = models.OneToOneField(
        'so.SalesOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='source_lpo_request',
        help_text="Set when lpo_agent (or a human via lpo_request_convert) successfully created a Sales "
                  "Order from the matched quotation.",
    )
    error = models.TextField(blank=True, default='')

    dismissed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    dismissed_at = models.DateTimeField(null=True, blank=True)
    dismiss_reason = models.CharField(max_length=255, blank=True, default='')

    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"LPO {self.lpo_number or '(no number)'} for email {self.tracked_email_id} ({self.status})"


class LPORequestItem(models.Model):
    """One line item extracted from an LPO PDF (see LPORequest). NOT
    matched against the so.Items catalog -- the auto-created SalesOrder is
    always built from the MATCHED QUOTATION's own items (via
    so.quotation_conversion_service), never from these lines directly.
    Exists purely for audit/display and as input to
    lpo_agent.find_matching_quotation's customer+item-overlap fallback
    matcher."""
    lpo_request = models.ForeignKey(LPORequest, on_delete=models.CASCADE, related_name='items')
    description = models.TextField()
    extra_description = models.TextField(
        blank=True, default='',
        help_text="Secondary description text in its own column/line on the LPO (e.g. 'WITH RUBBER' "
                  "under a 'GI HANGING CLAMP 6\"' item), distinct from the main `description`. Blank "
                  "if the LPO doesn't break these out separately.",
    )
    quantity = models.CharField(max_length=50, blank=True, default='')
    unit = models.CharField(max_length=50, blank=True, default='')
    price = models.FloatField(null=True, blank=True, help_text="Unit price, excl. VAT.")
    discount_percent = models.CharField(
        max_length=20, blank=True, default='',
        help_text="Line discount %, kept as free text (like quantity/unit) since LLM-extracted "
                  "formatting is unreliable to force into a stricter field.",
    )
    vat_amount = models.FloatField(null=True, blank=True, help_text="Line VAT amount, if broken out per line.")
    amount = models.FloatField(null=True, blank=True, help_text="Line total/extended amount.")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        return self.description[:60]


class EmailAgentSyncState(models.Model):
    """Singleton row (pk=1) tracking each MAILBOX's poll watermark -- kept
    on one row (rather than one per mailbox) since there's only ever one
    mailbox per source; separate field pairs let Gmail, the primary Outlook
    mailbox, and the project@junaid.ae mailbox poll independently without
    clobbering each other's position."""
    last_history_id = models.CharField(max_length=32, blank=True, default='')
    last_synced_internal_date = models.DateTimeField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)

    # IMAP has no history-id equivalent -- the highest UID processed so far
    # plays the same watermark role (IMAP UIDs are monotonically increasing
    # within a mailbox's UIDVALIDITY).
    last_outlook_uid = models.CharField(max_length=32, blank=True, default='')
    last_outlook_run_at = models.DateTimeField(null=True, blank=True)

    # Same watermark role as last_outlook_uid, but for the separate
    # project@junaid.ae mailbox (see emailagent.services.poll_project_mailbox)
    # -- a distinct field pair because it's a distinct IMAP mailbox with its
    # own independent UID sequence, not a filtered view of the same one.
    last_project_uid = models.CharField(max_length=32, blank=True, default='')
    last_project_run_at = models.DateTimeField(null=True, blank=True)

    # Same watermark role again, for the fourth mailbox (SUBMITTAL_IMAP_*,
    # see emailagent.services.poll_submittal_mailbox) -- its own independent
    # UID sequence, not a filtered view of any other mailbox's.
    last_submittal_uid = models.CharField(max_length=32, blank=True, default='')
    last_submittal_run_at = models.DateTimeField(null=True, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_instance(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"Gmail sync state (last_history_id={self.last_history_id or '—'})"


class AgentRun(models.Model):
    """Monitoring/observability record for one automated step in the RFQ
    pipeline: the Claude-driven agents (classify_email, draft_quotation,
    rematch_unmatched_items, recheck_item_brand_matches) plus non-AI steps
    worth the same visibility -- actually emailing a quotation to a client
    (send_quotation_email), and deterministic LPO matching/Sales Order
    creation (process_lpo, see emailagent/lpo_agent.py). Written by
    emailagent/supervisor.py, which only ever OBSERVES what a step produced
    (timing, success/failure, notable issues like unmatched items or a
    meters->pieces conversion) -- it never edits the TrackedEmail,
    EnquiryItem, Quotation, or LPORequest it watched."""
    AGENT_CLASSIFY = 'classify_email'
    AGENT_DRAFT = 'draft_quotation'
    AGENT_REMATCH = 'rematch_unmatched_items'
    AGENT_RECHECK = 'recheck_item_brand_matches'
    AGENT_SEND = 'send_quotation_email'
    AGENT_DRAFT_SUBMITTAL = 'draft_submittal'
    AGENT_SEND_SUBMITTAL = 'send_submittal_email'
    AGENT_PROCESS_LPO = 'process_lpo'
    AGENT_CHOICES = [
        (AGENT_CLASSIFY, 'Email Classification'),
        (AGENT_DRAFT, 'Quotation Drafting'),
        (AGENT_REMATCH, 'Unmatched Item Rematch'),
        (AGENT_RECHECK, 'Brand Match Recheck'),
        (AGENT_SEND, 'Quotation Sent to Client'),
        (AGENT_DRAFT_SUBMITTAL, 'Submittal Drafting'),
        (AGENT_SEND_SUBMITTAL, 'Submittal Sent to Client'),
        (AGENT_PROCESS_LPO, 'LPO Processing'),
    ]

    STATUS_SUCCESS = 'success'
    STATUS_FLAGGED = 'flagged'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_SUCCESS, 'Success'),
        (STATUS_FLAGGED, 'Flagged'),
        (STATUS_FAILED, 'Failed'),
    ]

    agent_name = models.CharField(max_length=40, choices=AGENT_CHOICES, db_index=True)
    tracked_email = models.ForeignKey(
        'TrackedEmail', on_delete=models.SET_NULL, null=True, blank=True, related_name='agent_runs',
    )
    quotation = models.ForeignKey(
        'so.Quotation', on_delete=models.SET_NULL, null=True, blank=True, related_name='agent_runs',
    )
    submittal = models.ForeignKey(
        'submittal.Submittal', on_delete=models.SET_NULL, null=True, blank=True, related_name='agent_runs',
    )
    sales_order = models.ForeignKey(
        'so.SalesOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='agent_runs',
    )

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_SUCCESS, db_index=True)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)

    summary = models.CharField(max_length=500, blank=True, default='')
    issues = models.JSONField(
        default=list, blank=True,
        help_text="Human-readable things worth double-checking, e.g. a unit conversion or a low-confidence match. "
                  "Purely informational -- nothing here has been auto-corrected.",
    )
    error = models.TextField(blank=True, default='')

    # Not auto_now_add -- the one-off backfill_agent_runs command needs to set
    # this to the real historical timestamp for runs that predate the
    # supervisor; auto_now_add would force it to "now" on every save
    # regardless of what's passed. Live-recorded runs get "now" anyway via
    # this default, since AgentRunRecorder never sets it explicitly.
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.get_agent_name_display()} [{self.status}] {self.created_at:%Y-%m-%d %H:%M}"


class StockShortageReport(models.Model):
    """SINGLETON (always pk=1, see `current()`) holding the CURRENT
    consolidated stock position across every "pending" LPO-sourced
    so.SalesOrder (source_lpo_request set, order_status not yet
    'SO Created') -- not a growing history of one snapshot per order.
    Recomputed FROM SCRATCH (see emailagent.stock_check.recompute) every
    time an LPO-sourced Sales Order is created (lpo_agent.
    match_and_maybe_convert / views.lpo_request_convert) or the page is
    manually refreshed (views.stock_shortage_report_refresh) -- since each
    recompute re-scans every pending order, a new LPO needing an
    already-listed item simply lands in that item's existing row/breakdown
    on the next recompute, rather than creating a separate report."""
    updated_at = models.DateTimeField(auto_now=True)
    last_triggered_by = models.ForeignKey(
        'so.SalesOrder', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text="The Sales Order whose creation (or a manual refresh, in which case this is "
                  "whatever last triggered it) most recently caused a recompute -- purely "
                  "informational, NOT part of this report's identity (there is only ever one row).",
    )
    lines = models.JSONField(
        default=list,
        help_text="One entry per short item, most-short first: item_code, brand, description, "
                  "total_required_qty (summed across every pending LPO-sourced order needing it), "
                  "available_qty (so.models.Items.total_available_stock, null if that item's stock "
                  "has never been synced), final_qty (the shortfall to procure = required - "
                  "available, null if available is unknown), already_ordered_qty (quantity still "
                  "outstanding on OPEN purchase orders WE placed with our own suppliers -- "
                  "so.models.SAPPurchaseOrderItem, opposite direction from a customer's LPO to us), "
                  "final_purchase_qty (what still needs a NEW supplier order = final_qty - "
                  "already_ordered_qty, floored at 0; null if final_qty is unknown), and "
                  "lpo_breakdown -- a list of "
                  "{lpo_id, lpo_number, sales_order_number, customer_name, quantity, payment_terms} "
                  "showing exactly which LPO(s)/customer(s) need how much of this item.",
    )
    stock_api_error = models.TextField(
        blank=True, default='',
        help_text="Set when one or more items have never had stock synced -- lines still list every "
                  "required item, with available_qty/final_qty left null (unknown) rather than being "
                  "silently dropped, so procurement still sees total demand even without a known "
                  "stock figure for that item.",
    )

    @classmethod
    def current(cls):
        """Gets (creating if this is the very first check ever) the one
        singleton row."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"Current stock shortage report ({len(self.lines)} item(s), updated {self.updated_at:%Y-%m-%d %H:%M})"
