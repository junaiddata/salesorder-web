"""Read-only: list the Alabama items whose code matches a Junaid item that has a photo / datasheet but whose description
differs, so an admin can decide which ones to link (admin > Alabama media links). Changes nothing."""
from django.core.management.base import BaseCommand

from alabama import media_match


class Command(BaseCommand):
    help = 'List Alabama items that could reuse a Junaid photo / datasheet but need a manual link (descriptions differ).'

    def handle(self, *args, **opts):
        rows = media_match.review_rows()
        for a, j in rows:
            self.stdout.write(f'{a.item_code} | Alabama: {a.item_description[:45]} [{a.item_firm}] | Junaid: {j.item_description[:45]} [{j.item_firm}]')
        self.stdout.write(f'{len(rows)} item(s) to review.')
