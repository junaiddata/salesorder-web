from django.db import models
from django.conf import settings
import os


def company_doc_path(instance, filename):
    return f'submittal/company_docs/{filename}'


def section_divider_path(instance, filename):
    return f'submittal/dividers/section_{instance.section_num}_{filename}'


def submittal_upload_path(instance, filename):
    return f'submittal/uploads/{instance.pk or "new"}/{filename}'


def generated_pdf_path(instance, filename):
    return f'submittal/generated/submittal_{instance.pk}.pdf'


def material_cert_path(instance, filename):
    brand_code = getattr(instance.material.brand, 'code', 'unknown') if instance.material.brand else 'unknown'
    return f'submittal/certifications/{brand_code}/{instance.material.model_no}/{instance.cert_type}/{filename}'


def catalogue_upload_path(instance, filename):
    brand_code = getattr(instance.brand, 'code', 'unknown') if instance.brand else 'unknown'
    return f'submittal/catalogue/{brand_code}/{instance.model_no}/{filename}'


class SubmittalBrand(models.Model):
    """
    Brand for submittal materials. Each brand has its own column definitions.
    """
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=50, unique=True, db_index=True)
    column_definitions = models.JSONField(
        default=list, blank=True,
        help_text='[{"key": "model_no", "label": "Model No.", "order": 1}, ...]'
    )
    display_order = models.IntegerField(default=0)
    use_generated_warranty = models.BooleanField(
        default=False,
        help_text="When enabled, warranty letter is auto-generated from materials table instead of PDF upload. Configure in Admin."
    )

    class Meta:
        ordering = ['display_order', 'name']

    def __str__(self):
        return self.name


class CompanyDocuments(models.Model):
    """
    Singleton-style model: stores company-wide PDFs uploaded once,
    reused across all submittals (index standard, company profile, trade license).
    """
    index_standard_pdf = models.FileField(
        upload_to=company_doc_path, blank=True, null=True,
        help_text="Standard index format PDF (Section 2)"
    )
    company_profile_pdf = models.FileField(
        upload_to=company_doc_path, blank=True, null=True,
        help_text="Company profile PDF (Section 3)"
    )
    trade_license_pdf = models.FileField(
        upload_to=company_doc_path, blank=True, null=True,
        help_text="Trade license PDF (Section 4)"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Company Documents"
        verbose_name_plural = "Company Documents"

    def __str__(self):
        return f"Company Documents (updated {self.updated_at:%Y-%m-%d})" if self.updated_at else "Company Documents"

    @classmethod
    def get_instance(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class SectionDivider(models.Model):
    """
    Cover/divider page inserted before each section content.
    One PDF per section, uploaded once via Admin.
    """
    SECTION_CHOICES = [
        (2, 'Index'),
        (3, 'Company Profile'),
        (4, 'Trade License'),
        (5, 'Highlighted Vendor List'),
        (6, 'Comply Statement'),
        (7, 'List of Proposed Material'),
        (8, 'Area of Application'),
        (9, 'Product Catalogue'),
        (10, 'Technical Details'),
        (11, 'Test Certificates'),
        (12, 'Country of Origin Certificate'),
        (13, 'Warranty Draft Letter'),
        (14, 'Previous Approvals'),
    ]

    section_num = models.IntegerField(unique=True, choices=SECTION_CHOICES)
    section_name = models.CharField(max_length=100)
    divider_pdf = models.FileField(upload_to=section_divider_path)

    class Meta:
        ordering = ['section_num']
        verbose_name = "Section Divider"

    def __str__(self):
        return f"{self.section_num}. {self.section_name}"


class SubmittalMaterial(models.Model):
    """
    Master list of materials (models) available for submittals.
    Each material belongs to a brand; attribute columns are stored in data JSON.
    Legacy fields (item_description, material, etc.) kept for migration only.
    """
    brand = models.ForeignKey(
        SubmittalBrand, on_delete=models.CASCADE, related_name='materials',
        help_text="Brand (e.g. Pegler)"
    )
    model_no = models.CharField(max_length=100, db_index=True, help_text="Model No. (e.g. 10751, V8850)")
    data = models.JSONField(
        default=dict, blank=True,
        help_text="Attribute columns as key-value, e.g. {item_description, material, size, ...}"
    )

    catalogue_pdf = models.FileField(
        upload_to=catalogue_upload_path, blank=True, null=True,
        help_text="Product catalogue PDF (Section 9)"
    )
    technical_pdf = models.FileField(
        upload_to=catalogue_upload_path, blank=True, null=True,
        help_text="Technical details PDF (Section 10)"
    )

    display_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['display_order', 'model_no']
        unique_together = [('brand', 'model_no')]

    def __str__(self):
        desc = (self.data or {}).get('item_description', '')
        return f"{self.model_no} - {desc}" if desc else str(self.model_no)

    def get(self, key, default=''):
        """Get attribute from data."""
        return (self.data or {}).get(key, default)


class MaterialCertification(models.Model):
    """
    Stores multiple certificate files per material.
    Types: test_certificate, country_of_origin, previous_approval
    """
    CERT_TYPE_CHOICES = [
        ('test_certificate', 'Test Certificate'),
        ('country_of_origin', 'Country of Origin'),
        ('previous_approval', 'Previous Approval'),
    ]

    material = models.ForeignKey(
        SubmittalMaterial, on_delete=models.CASCADE, related_name='certifications'
    )
    cert_type = models.CharField(max_length=30, choices=CERT_TYPE_CHOICES)
    file = models.FileField(upload_to=material_cert_path)
    description = models.CharField(max_length=255, blank=True, default='')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['material', 'cert_type', 'uploaded_at']

    def __str__(self):
        return f"{self.material.model_no} - {self.get_cert_type_display()} - {self.description or self.file.name}"


class ProjectContractorHistory(models.Model):
    """
    Stores previous project/contractor values for dropdown auto-complete.
    One record created per submittal.
    """
    project = models.TextField(blank=True, default='')
    client = models.CharField(max_length=255, blank=True, default='')
    consultant = models.CharField(max_length=255, blank=True, default='')
    main_contractor = models.CharField(max_length=255, blank=True, default='')
    mep_contractor = models.CharField(max_length=255, blank=True, default='')
    product = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = "Project/Contractor History"
        verbose_name_plural = "Project/Contractor History"

    def __str__(self):
        return f"{self.project[:50]} ({self.created_at:%Y-%m-%d})" if self.project else f"Entry {self.pk}"


def section_upload_path(instance, filename):
    return f'submittal/section_uploads/{instance.submittal_id or "new"}/{filename}'


class Submittal(models.Model):
    """Main submittal document combining all sections."""

    # Section 1 - Title Page
    project = models.TextField(help_text="Project name/description")
    client = models.CharField(max_length=255, blank=True, default='')
    consultant = models.CharField(max_length=255, blank=True, default='')
    main_contractor = models.CharField(max_length=255, blank=True, default='')
    mep_contractor = models.CharField(max_length=255, blank=True, default='')
    product = models.CharField(max_length=255, blank=True, default='')

    # Section 2 - Index (ordered list of {label, included, display_label?} dicts, generated with ReportLab)
    index_items = models.JSONField(
        default=list, blank=True,
        help_text="Ordered list of index entries: [{label, included, display_label?}]. display_label overrides label for this submittal."
    )

    # Section 5 - Vendor List
    vendor_list_pdf = models.FileField(
        upload_to=submittal_upload_path, blank=True, null=True,
        help_text="Highlighted vendor list PDF (optional)"
    )

    # Section 6 - Comply Statement
    comply_statement_file = models.FileField(
        upload_to=submittal_upload_path, blank=True, null=True,
        help_text="Comply statement PDF/Word"
    )

    # Section 7 - Proposed Materials
    materials = models.ManyToManyField(SubmittalMaterial, blank=True, related_name='submittals')
    materials_columns = models.JSONField(
        default=list, blank=True,
        help_text="Column keys to show in materials table. Empty = show all."
    )

    # Section 8 - Area of Application
    area_of_application_pdf = models.FileField(
        upload_to=submittal_upload_path, blank=True, null=True,
        help_text="Area of application PDF (optional)"
    )

    # Section 13 - Warranty Draft
    warranty_draft_pdf = models.FileField(
        upload_to=submittal_upload_path, blank=True, null=True,
        help_text="Warranty draft letter PDF (placeholder)"
    )
    warranty_brand = models.ForeignKey(
        'SubmittalBrand', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='warranty_submittals',
        help_text="Brand with generated warranty format. When set and brand has use_generated_warranty, letter is auto-generated. Otherwise use PDF upload."
    )
    warranty_date_type = models.CharField(
        max_length=20, default='toc', blank=True,
        choices=[('toc', 'Date of TOC'), ('invoice', 'Date of Invoice')],
        help_text="Warranty period wording: from date of TOC or Invoice"
    )
    warranty_materials_columns = models.JSONField(
        default=list, blank=True,
        help_text="Column keys for warranty materials table. Empty = use materials_columns."
    )

    # Section 6 - Compliance Statement (form-based rows, optional)
    compliance_rows = models.JSONField(
        default=list, blank=True,
        help_text="Compliance statement rows: [{specification, compliance, remarks}, ...]"
    )
    compliance_brand = models.ForeignKey(
        'SubmittalBrand', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='compliance_submittals',
        help_text="Brand used for remark options in compliance statement"
    )

    # Stored output PDF — generated once, temp uploads deleted after
    generated_pdf = models.FileField(
        upload_to=generated_pdf_path, blank=True, null=True,
        help_text="Final merged PDF; temp uploads are deleted after generation"
    )
    pdf_generated_at = models.DateTimeField(blank=True, null=True, help_text="When the PDF was last generated")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Submittal: {self.project[:60]} ({self.created_at:%Y-%m-%d})" if self.created_at else f"Submittal: {self.project[:60]}"


class SubmittalSectionUpload(models.Model):
    """
    Per-submittal uploaded PDF for a specific index section.
    Used for custom sections and standard sections that need per-submittal content
    (vendor list, comply statement, area of application, warranty draft, etc.).
    """
    submittal = models.ForeignKey(Submittal, on_delete=models.CASCADE, related_name='section_uploads')
    index_label = models.CharField(max_length=255, help_text="Must match the index item label exactly")
    file = models.FileField(upload_to=section_upload_path)
    uploaded_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('submittal', 'index_label')
        ordering = ['submittal', 'index_label']

    def __str__(self):
        return f"{self.submittal_id} - {self.index_label}"


# ---------------------------------------------------------------------------
# Compliance Statement options
# ---------------------------------------------------------------------------

class ComplianceOption(models.Model):
    """Global options for the Compliance dropdown in the compliance statement form."""
    label = models.CharField(max_length=255, unique=True)
    display_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['display_order', 'label']
        verbose_name = "Compliance Option"
        verbose_name_plural = "Compliance Options"

    def __str__(self):
        return self.label


class RemarkOption(models.Model):
    """Brand-specific options for the Remarks dropdown in the compliance statement form."""
    brand = models.ForeignKey(
        SubmittalBrand, on_delete=models.CASCADE, related_name='remark_options',
        help_text="Brand this remark belongs to"
    )
    label = models.TextField(help_text="Remark text (can be multi-line)")
    display_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['brand', 'display_order', 'label']
        verbose_name = "Remark Option"
        verbose_name_plural = "Remark Options"

    def __str__(self):
        return f"{self.brand.name}: {self.label[:60]}"
