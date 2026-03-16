from .models import CompanyDocuments, SubmittalMaterial, MaterialCertification


def get_company_documents():
    """Return the singleton CompanyDocuments instance."""
    return CompanyDocuments.get_instance()


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
    """Return distinct previous values for a title-page field (unique only)."""
    from .models import ProjectContractorHistory
    seen = set()
    result = []
    for v in ProjectContractorHistory.objects.exclude(**{field_name: ''}).order_by('-pk').values_list(field_name, flat=True)[:200]:
        if v and v not in seen:
            seen.add(v)
            result.append(v)
    return result[:100]
