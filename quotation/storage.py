"""Private storage for quotation item photos and datasheets.

Files live OUTSIDE MEDIA_ROOT, so Django/nginx never serves them by path. Photos
are read by the PDF builder and the staff-only preview view; datasheets are only
reachable through the tokenised public view.
"""
import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage


class PrivateQuotationStorage(FileSystemStorage):
    """FileSystemStorage whose folder is read lazily (so tests can override it) and
    which never exposes a URL."""

    @property
    def base_location(self):
        return getattr(settings, 'QUOTATION_PRIVATE_ROOT',
                       os.path.join(settings.BASE_DIR, 'private_media', 'quotation'))

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    def url(self, name):
        raise ValueError('Quotation files are private and have no URL.')


def get_private_storage():
    return PrivateQuotationStorage()
