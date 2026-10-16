"""Private storage for the company signature and stamp.

Files live OUTSIDE MEDIA_ROOT, so Django/nginx never serves them by URL. They
are only read server-side by the PDF builder.
"""
import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage


class PrivateSealStorage(FileSystemStorage):
    """FileSystemStorage whose folder is read lazily (so tests can override it) and
    which never exposes a URL."""

    @property
    def base_location(self):
        return getattr(settings, 'BANGLADESH_PI_PRIVATE_ROOT',
                       os.path.join(settings.BASE_DIR, 'private_media', 'bangladeshpi'))

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    def url(self, name):
        raise ValueError('Signature and stamp files are private and have no URL.')


def get_private_storage():
    return PrivateSealStorage()
