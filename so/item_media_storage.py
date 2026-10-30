"""Private storage for the item catalog photos and datasheets.

Files live OUTSIDE MEDIA_ROOT (private_media/items/), so Django/nginx never serves them by path; they are only
reachable through the login-protected catalog views.
"""
import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage


class ItemMediaStorage(FileSystemStorage):
    """FileSystemStorage whose folder is read lazily (so tests can override it) and which never exposes a URL."""

    @property
    def base_location(self):
        return getattr(settings, 'ITEM_MEDIA_PRIVATE_ROOT',
                       os.path.join(settings.BASE_DIR, 'private_media', 'items'))

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    def url(self, name):
        raise ValueError('Item catalog files are private and have no URL.')


def get_item_media_storage():
    return ItemMediaStorage()
