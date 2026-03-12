from django.db import models
from django.conf import settings
import os


def company_doc_path(instance, filename):
    return f'submittal/company_docs/{filename}'


def section_divider_path(instance, filename):
    return f'submittal/dividers/section_{instance.section_num}_{filename}'


def submittal_upload_path(instance, filename):
    return f'submittal/uploads/{instance.pk or "new"}/{filename}'


def material_cert_path(instance, filename):
    return f'submittal/certifications/{instance.material.item_code}/{instance.cert_type}/{filename}'


def catalogue_upload_path(instance, filename):
    return f'submittal/catalogue/{instance.item_code}/{filename}'


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
    Master list of materials available for submittals.
    Each material has its own catalogue, technical details, and certifications.
    """
    item_code = models.CharField(max_length=100, unique=True, db_index=True)
    description = models.CharField(max_length=255)
    brand = models.CharField(max_length=100, blank=True, default='')
    size = models.CharField(max_length=100, blank=True, default='')
    wras_number = models.CharField(max_length=100, blank=True, default='', help_text="WRAS certification number")
    other_certifications = models.TextField(blank=True, default='', help_text="Other cert numbers, comma-separated")

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
        ordering = ['display_order', 'description']

    def __str__(self):
        return f"{self.item_code} - {self.description}"


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
        return f"{self.material.item_code} - {self.get_cert_type_display()} - {self.description or self.file.name}"


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


class Submittal(models.Model):
    """Main submittal document combining all sections."""

    # Section 1 - Title Page
    project = models.TextField(help_text="Project name/description")
    client = models.CharField(max_length=255, blank=True, default='')
    consultant = models.CharField(max_length=255, blank=True, default='')
    main_contractor = models.CharField(max_length=255, blank=True, default='')
    mep_contractor = models.CharField(max_length=255, blank=True, default='')
    product = models.CharField(max_length=255, blank=True, default='')

    # Section 2 - Index
    INDEX_FORMAT_CHOICES = [
        ('standard', 'Standard Format'),
        ('client', 'Client Format'),
    ]
    index_format = models.CharField(max_length=10, choices=INDEX_FORMAT_CHOICES, default='standard')
    index_client_pdf = models.FileField(
        upload_to=submittal_upload_path, blank=True, null=True,
        help_text="Client-provided index PDF"
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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Submittal: {self.project[:60]} ({self.created_at:%Y-%m-%d})" if self.created_at else f"Submittal: {self.project[:60]}"
