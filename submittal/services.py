from .models import (
    CompanyDocuments, SectionDivider, SubmittalMaterial, MaterialCertification,
)


def get_company_documents():
    """Return the singleton CompanyDocuments instance."""
    return CompanyDocuments.get_instance()


def get_divider_pdf(section_num):
    """Return the file path for a section divider, or None."""
    try:
        divider = SectionDivider.objects.get(section_num=section_num)
        if divider.divider_pdf:
            return divider.divider_pdf.path
    except SectionDivider.DoesNotExist:
        pass
    return None


def get_catalogue_pdf(material):
    """Return the catalogue PDF path for a material, or None."""
    if material.catalogue_pdf:
        return material.catalogue_pdf.path
    return None


def get_technical_pdf(material):
    """Return the technical details PDF path for a material, or None."""
    if material.technical_pdf:
        return material.technical_pdf.path
    return None


def get_certifications(material, cert_type):
    """Return list of file paths for a given material and cert_type."""
    certs = MaterialCertification.objects.filter(
        material=material, cert_type=cert_type
    )
    return [c.file.path for c in certs if c.file]


def get_history_values(field_name):
    """Return distinct previous values for a title-page field, most recent first."""
    from .models import ProjectContractorHistory
    return list(
        ProjectContractorHistory.objects
        .exclude(**{field_name: ''})
        .values_list(field_name, flat=True)
        .distinct()
        .order_by(f'-pk')[:50]
    )
