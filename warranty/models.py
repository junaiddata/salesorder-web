from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

from .storage_backends import generated_pdf_storage


def warranty_signature_path(instance, filename):
    return f'warranty/signatures/{instance.pk or "new"}/{filename}'


def warranty_stamp_path(instance, filename):
    return f'warranty/stamps/{instance.pk or "new"}/{filename}'


def warranty_generated_pdf_path(instance, filename):
    return f'warranty/generated/warranty_{instance.pk}.pdf'


def warranty_settings_asset_path(instance, filename):
    return f'warranty/settings/{instance.company}/{filename}'


def default_item_columns():
    """Starting columns for a brand-new letter's item table -- freely
    add/remove/rename from here per letter via the form UI."""
    return [
        {'key': 'date', 'label': 'Date'},
        {'key': 'invoice_no', 'label': 'Invoice No.'},
        {'key': 'lpo_no', 'label': 'LPO No.'},
        {'key': 'brand_model', 'label': 'Brand/Model'},
        {'key': 'total_qty', 'label': 'Total Qty.'},
    ]


class WarrantyLetterSettings(models.Model):
    """
    One row per company (junaid/alabama). Admin-configurable letterhead
    assets and defaults copied into every new WarrantyLetter, mirroring
    submittal.CompanyDocuments.get_instance() but keyed by company instead
    of being a true singleton.
    """
    COMPANY_CHOICES = [
        ('junaid', 'Junaid'),
        ('alabama', 'Alabama'),
    ]

    company = models.CharField(max_length=10, choices=COMPANY_CHOICES, unique=True)
    legal_company_name = models.CharField(max_length=255, blank=True, default='')
    ref_no_prefix = models.CharField(
        max_length=30, blank=True, default='',
        help_text="e.g. JN/WC or AL/WC — used to build the auto-suggested Ref No."
    )
    header_logo = models.ImageField(
        upload_to=warranty_settings_asset_path, blank=True, null=True,
        help_text="Top-right logo drawn on every page of the generated letter."
    )
    footer_image = models.ImageField(
        upload_to=warranty_settings_asset_path, blank=True, null=True,
        help_text="Full-width footer banner (certification badges + address block) "
                  "drawn at the bottom of every page. Upload a single ready-made image."
    )
    default_signatory_name = models.CharField(max_length=255, blank=True, default='')
    default_signatory_title = models.CharField(max_length=255, blank=True, default='')
    default_intro_template = models.TextField(
        blank=True, default='',
        help_text="Copied into every new letter's editable Intro Text. "
                  "Supports {brand} and {iso_standard} placeholders."
    )
    default_terms_text = models.TextField(
        blank=True, default='',
        help_text="Copied into every new letter's editable Terms. One bullet per line."
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Warranty Letter Settings"
        verbose_name_plural = "Warranty Letter Settings"

    def __str__(self):
        return f"Warranty Letter Settings ({self.get_company_display()})"

    @classmethod
    def get_instance(cls, company):
        company = company if company in dict(cls.COMPANY_CHOICES) else 'junaid'
        obj, _ = cls.objects.get_or_create(company=company)
        return obj


class WarrantyLetter(models.Model):
    COMPANY_CHOICES = WarrantyLetterSettings.COMPANY_CHOICES

    STATUS_CHOICES = [
        ('Draft', 'Draft'),
        ('Pending', 'Pending Approval'),
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected'),
    ]
    # Statuses "Submit for Approval" can be used from.
    SUBMITTABLE_STATUSES = ('Draft', 'Rejected')

    company = models.CharField(max_length=10, choices=COMPANY_CHOICES, default='junaid')
    ref_no = models.CharField(
        max_length=50, blank=True, default='',
        help_text="Auto-suggested per company + year, fully editable."
    )
    letter_date = models.DateField(default=timezone.now)

    project = models.TextField(blank=True, default='')
    client = models.CharField(max_length=255, blank=True, default='')
    consultant = models.CharField(max_length=255, blank=True, default='')
    main_contractor = models.CharField(max_length=255, blank=True, default='')

    brand = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Free text — this app is standalone, not tied to submittal brands."
    )
    iso_standard = models.CharField(max_length=100, blank=True, default='ISO 9001:2015')
    intro_text = models.TextField(
        blank=True, default='',
        help_text="Editable intro sentence naming the brand and quality standard."
    )

    terms_text = models.TextField(
        blank=True, default='',
        help_text="Warranty terms — one bullet point per line, any number of lines."
    )

    item_columns = models.JSONField(
        default=default_item_columns, blank=True,
        help_text="Ordered item-table columns: [{key, label}, ...]. Freely customizable per letter."
    )

    signatory_name = models.CharField(max_length=255, blank=True, default='')
    signatory_title = models.CharField(max_length=255, blank=True, default='')

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='Draft')

    submitted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='warranty_letters_submitted'
    )
    submitted_at = models.DateTimeField(null=True, blank=True)

    approval_updated_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='warranty_letters_approved'
    )
    approval_updated_at = models.DateTimeField(null=True, blank=True)
    approval_remarks = models.TextField(
        blank=True, default='', help_text="Manager's comments left when approving/rejecting."
    )

    signature_image = models.ImageField(upload_to=warranty_signature_path, blank=True, null=True)
    stamp_image = models.ImageField(upload_to=warranty_stamp_path, blank=True, null=True)

    generated_pdf = models.FileField(
        upload_to=warranty_generated_pdf_path, blank=True, null=True,
        storage=generated_pdf_storage,
        help_text="Cached final PDF; regenerated lazily whenever the letter changes."
    )
    pdf_generated_at = models.DateTimeField(blank=True, null=True)

    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='warranty_letters_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.ref_no or 'Draft'} - {self.project[:40]}"

    def invalidate_pdf(self):
        if self.generated_pdf:
            self.generated_pdf.delete(save=False)
        self.generated_pdf = None
        self.pdf_generated_at = None


class WarrantyLetterItem(models.Model):
    """One row of the letter's item table. Columns are freely customizable
    per letter (see WarrantyLetter.item_columns), so row values are stored
    as a key->value JSON dict keyed by each column's key, rather than fixed
    model fields."""
    letter = models.ForeignKey(WarrantyLetter, on_delete=models.CASCADE, related_name='items')
    display_order = models.IntegerField(default=0)

    data = models.JSONField(
        default=dict, blank=True,
        help_text="Row values as key-value, keyed by the letter's item_columns keys."
    )

    class Meta:
        ordering = ['display_order', 'pk']

    def __str__(self):
        return f"Letter {self.letter_id} row {self.display_order}"

    def get(self, key, default=''):
        return (self.data or {}).get(key, default)
