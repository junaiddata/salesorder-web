"""
Storage backends for the submittal app.

Generated submittal PDFs are stored on the local filesystem via default_storage.
"""
from django.core.files.storage import default_storage


def generated_pdf_storage():
    """Return local default storage for generated submittal PDFs."""
    return default_storage
