"""Shared PDF post-processing for the quotation generators."""
import logging

log = logging.getLogger(__name__)


def clean_pdf(data):
    """Rewrite a finished PDF as a clean, self-consistent file and return the bytes.

    The quotation PDFs are made by merging a Canva letterhead onto ReportLab pages with PyPDF2. That leaves tagged-content
    markers whose structure tree was dropped, in a file with a PDF 1.3 header. Most viewers ignore this, but Adobe Acrobat
    reports "There was a problem reading this document (14)". Re-saving through PyMuPDF normalises the file.

    Never raises: if anything goes wrong the original bytes are returned unchanged, so an export or email never breaks.
    """
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(stream=data, filetype='pdf')
        try:
            if doc.page_count < 1:
                return data
            cleaned = doc.tobytes(garbage=4, deflate=True)
        finally:
            doc.close()
        if not cleaned.startswith(b'%PDF'):
            return data
        return cleaned
    except Exception:
        log.exception('PDF clean-up failed; returning the original PDF')
        return data
