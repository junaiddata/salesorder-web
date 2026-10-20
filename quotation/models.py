import secrets
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from .storage import get_private_storage

VAT_RATE = Decimal('0.05')   # UAE VAT, added on top of the item total
CENT = Decimal('0.01')


def photo_upload_to(instance, filename):
    return f'photos/{secrets.token_hex(8)}_{filename}'


def datasheet_upload_to(instance, filename):
    return f'datasheets/{secrets.token_hex(8)}_{filename}'


def new_share_token():
    return secrets.token_urlsafe(24)


class Quotation(models.Model):
    """Customer quotation (AED) printed on the Junaid Galleria letterhead."""

    qtn_no = models.CharField(max_length=60, unique=True)
    date = models.DateField(default=timezone.localdate)
    to_text = models.TextField(blank=True)
    remarks = models.TextField(blank=True)
    project_reference = models.CharField(max_length=300, blank=True)
    validity = models.CharField(max_length=200, blank=True)
    stock_availability = models.DateField(null=True, blank=True)
    country_of_origin = models.CharField(max_length=200, blank=True)
    sales_person = models.CharField(max_length=150, blank=True)
    email = models.CharField(max_length=200, blank=True)
    contact_number = models.CharField(max_length=100, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def total(self):
        """Sum of the item amounts, before VAT."""
        return sum((i.amount for i in self.items.all()), Decimal('0'))

    @property
    def vat_amount(self):
        return (self.total * VAT_RATE).quantize(CENT, rounding=ROUND_HALF_UP)

    @property
    def grand_total(self):
        return self.total + self.vat_amount

    def __str__(self):
        return self.qtn_no


class QuotationItem(models.Model):
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name='items')
    position = models.PositiveIntegerField(default=0)
    # Typed by the user (e.g. H12); blank = the row number is printed instead.
    item_no = models.CharField(max_length=50, blank=True)
    model_no = models.CharField(max_length=100, blank=True)
    photo = models.ImageField(upload_to=photo_upload_to, storage=get_private_storage, blank=True, null=True)
    brand = models.CharField(max_length=150, blank=True)
    description = models.TextField(blank=True)
    size = models.CharField(max_length=150, blank=True)
    color = models.CharField(max_length=100, blank=True)
    qty = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    unit_price = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    warranty = models.CharField(max_length=150, blank=True)
    # Datasheet (always stored as a JPEG, whatever was uploaded), shared with customers through /quote-builder/ds/<share_token>/
    # (the download link in the quotation PDF). The token stays the same when the file
    # is replaced, so quotations that were already sent keep working.
    datasheet = models.FileField(upload_to=datasheet_upload_to, storage=get_private_storage, blank=True, null=True)
    share_token = models.CharField(max_length=64, unique=True, null=True, blank=True)

    class Meta:
        ordering = ['position', 'id']

    @property
    def amount(self):
        # Rounded to the cent, so the printed amounts always add up to the printed total.
        return ((self.qty or Decimal('0')) * (self.unit_price or Decimal('0'))).quantize(CENT, rounding=ROUND_HALF_UP)
