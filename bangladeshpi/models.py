from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from .storage import get_private_storage

REF_PREFIX = 'BD/IMAA/2026/'


class BangladeshPI(models.Model):
    """Proforma Invoice for the Bangladesh (IMAA Corporation) export business."""

    ref_suffix = models.CharField(
        max_length=40, unique=True,
        help_text='The part of the Ref No after BD/IMAA/2026/ (e.g. 07A)',
    )
    date = models.DateField(default=timezone.localdate)
    port_of_origin = models.CharField(max_length=200, blank=True)
    country_of_origin = models.CharField(max_length=200, blank=True)
    transport = models.CharField(max_length=300, blank=True)
    insurance = models.CharField(max_length=300, blank=True)
    destination = models.CharField(max_length=300, blank=True)
    final_destination = models.CharField(max_length=300, blank=True)
    shipment = models.CharField(max_length=300, blank=True)
    validity = models.CharField(max_length=200, blank=True)
    payment = models.CharField(max_length=400, blank=True)
    freight_charges = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    freight_valid_till = models.CharField(max_length=100, blank=True)
    # Ticked = both PDFs/Excel titles read "PROFORMA INVOICE (Revised)".
    is_revised = models.BooleanField(default=False)
    # Bank copy freight: DEDUCTED from the bank total. 0 = the Bank PDF/Excel keep their single total line.
    bank_freight_charges = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    # Bank copy only: Terms & Conditions for issuing L/C, plus optional wording
    # overrides (blank = use the main Payment / Insurance text).
    bank_terms_text = models.TextField(blank=True)
    bank_payment = models.CharField(max_length=400, blank=True)
    bank_insurance = models.CharField(max_length=300, blank=True)
    # Digital signature / stamp (images are kept once in BangladeshPISeal;
    # these flags choose whether this PI's PDFs print them).
    add_signature = models.BooleanField(default=False)
    add_stamp = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def ref_no(self):
        return f'{REF_PREFIX}{self.ref_suffix}'

    @property
    def items_total(self):
        return sum((i.amount for i in self.items.all() if i.kind == 'item'), Decimal('0'))

    @property
    def grand_total(self):
        return self.items_total + (self.freight_charges or Decimal('0'))

    @property
    def bank_items_total(self):
        return sum((i.bank_amount for i in self.items.all() if i.kind == 'item'), Decimal('0'))

    @property
    def bank_grand_total(self):
        """Bank total as printed: bank items MINUS the bank freight (0 freight = items total)."""
        return self.bank_items_total - (self.bank_freight_charges or Decimal('0'))

    @property
    def has_bank_prices(self):
        """Bank copy is available only when every item row has a bank price."""
        # FOC rows are free on both copies, so they never need a bank price.
        rows = [i for i in self.items.all() if i.kind == 'item' and not i.is_foc]
        return bool(rows) and all(i.bank_unit_price is not None for i in rows)

    def __str__(self):
        return self.ref_no


class BangladeshPIItem(models.Model):
    KIND_CHOICES = [('item', 'Item'), ('heading', 'Group heading')]

    pi = models.ForeignKey(BangladeshPI, on_delete=models.CASCADE, related_name='items')
    position = models.PositiveIntegerField(default=0)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default='item')
    code = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True)
    qty = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    unit_price = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    bank_unit_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    # Free of charge: printed as "FOC" instead of a price/amount on both the
    # customer and the bank copy, and counted as 0 in every total.
    is_foc = models.BooleanField(default=False)

    class Meta:
        ordering = ['position', 'id']

    @property
    def amount(self):
        if self.is_foc:
            return Decimal('0')
        return (self.qty or Decimal('0')) * (self.unit_price or Decimal('0'))

    @property
    def bank_amount(self):
        if self.is_foc:
            return Decimal('0')
        return (self.qty or Decimal('0')) * (self.bank_unit_price or Decimal('0'))


def signature_upload_to(instance, filename):
    return f'bangladeshpi/seals/signature/{filename}'


def stamp_upload_to(instance, filename):
    return f'bangladeshpi/seals/stamp/{filename}'


class BangladeshPISeal(models.Model):
    """Single row holding the company signature and stamp images."""

    # Private storage: outside MEDIA_ROOT, never served by URL.
    signature = models.ImageField(upload_to=signature_upload_to, storage=get_private_storage, blank=True, null=True)
    stamp = models.ImageField(upload_to=stamp_upload_to, storage=get_private_storage, blank=True, null=True)

    @classmethod
    def get_instance(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class BangladeshCommercialInvoice(models.Model):
    """Commercial invoice made from a Bangladesh PI's bank copy.

    Everything that comes from the PI (invoice no, items, bank prices, bank freight,
    PI ref/date, origin) is COPIED here when the invoice is created, so editing or
    re-saving the PI later never changes an issued invoice. "Refresh from PI" re-copies.
    """

    pi = models.OneToOneField(BangladeshPI, on_delete=models.CASCADE, related_name='commercial_invoice')
    # copied from the PI
    invoice_no = models.CharField(max_length=60)
    pi_ref_no = models.CharField(max_length=80)
    pi_date = models.DateField(null=True, blank=True)
    country_of_origin = models.CharField(max_length=200, blank=True)
    hs_code = models.CharField(max_length=60, blank=True)
    freight_charges = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    # typed by the user
    invoice_date = models.DateField(default=timezone.localdate)
    comm_ref_no = models.CharField(max_length=60, blank=True)
    # packing list only
    pl_no = models.CharField(max_length=80, blank=True)
    issuing_bank_bin = models.CharField(max_length=40, blank=True)   # blank = the fixed default on the PDFs
    our_delivery = models.CharField(max_length=60, blank=True)
    ref_our_ord_no = models.CharField(max_length=60, blank=True)
    to_order_of = models.TextField(blank=True)
    packages = models.CharField(max_length=40, blank=True)
    lc_number = models.CharField(max_length=80, blank=True)
    lc_date = models.DateField(null=True, blank=True)
    container_no = models.CharField(max_length=60, blank=True)
    gross_weight = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    net_weight = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    volume = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    add_signature = models.BooleanField(default=False)
    add_stamp = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Commercial invoice {self.invoice_no}'

    @property
    def cfr_total(self):
        return sum((l.amount for l in self.lines.all() if l.kind == 'item'), Decimal('0'))

    @property
    def fob_total(self):
        return self.cfr_total - (self.freight_charges or Decimal('0'))


class BangladeshCommercialInvoiceLine(models.Model):
    ci = models.ForeignKey(BangladeshCommercialInvoice, on_delete=models.CASCADE, related_name='lines')
    position = models.PositiveIntegerField(default=0)
    kind = models.CharField(max_length=10, default='item')       # 'item' or 'heading'
    code = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True)
    qty = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    unit_price = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))   # the bank price
    pack = models.PositiveIntegerField(null=True, blank=True)
    gross_kg = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    net_kg = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ['position', 'id']

    @property
    def amount(self):
        return (self.qty or Decimal('0')) * (self.unit_price or Decimal('0'))
