import secrets
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from .storage import get_private_storage

PAYMENT_TERM_CHOICES = ['LC', 'PDC', 'Advance']
DELIVERY_SUBJECT_TO_STOCK = 'Subject to Stock availability'
DEFAULT_VALIDITY = '10 Days'
QTN_PREFIX = 'JG'          # quotation numbers look like JG-2026-001
VAT_RATE = Decimal('0.05')   # UAE VAT, added on top of the item total
CENT = Decimal('0.01')


def photo_upload_to(instance, filename):
    return f'photos/{secrets.token_hex(8)}_{filename}'


def datasheet_upload_to(instance, filename):
    return f'datasheets/{secrets.token_hex(8)}_{filename}'


def stamp_upload_to(instance, filename):
    return f'stamp/{secrets.token_hex(8)}_{filename}'


def line_amount(qty, unit_price):
    """qty x unit price, rounded to the cent so the printed lines always add up to the printed total."""
    return ((qty or Decimal('0')) * (unit_price or Decimal('0'))).quantize(CENT, rounding=ROUND_HALF_UP)


def new_share_token():
    return secrets.token_urlsafe(24)


class Quotation(models.Model):
    """Customer quotation (AED) printed on the Junaid Galleria letterhead."""

    qtn_no = models.CharField(max_length=60, unique=True)
    date = models.DateField(default=timezone.localdate)
    # The customer the quotation is addressed to. `to_text` is the older free-text box, kept as "other details"
    # so quotations made before these fields existed lose nothing.
    customer_name = models.CharField(max_length=200, blank=True)
    customer_contact = models.CharField(max_length=200, blank=True)
    customer_address = models.TextField(blank=True)
    customer_trn = models.CharField(max_length=50, blank=True)
    customer_person = models.CharField(max_length=200, blank=True)   # the contact person at the customer
    customer_code = models.CharField(max_length=50, blank=True)   # the customer's ID / account code
    customer_email = models.CharField(max_length=300, blank=True)   # where the quotation is emailed (several addresses: comma separated)
    to_text = models.TextField(blank=True)
    remarks = models.TextField(blank=True)
    customer_ref = models.CharField(max_length=200, blank=True)   # the customer's own reference (RFQ / enquiry no.)
    project_reference = models.CharField(max_length=300, blank=True)
    validity = models.CharField(max_length=200, blank=True)
    stock_availability = models.DateField(null=True, blank=True)
    # Print "Subject to Stock availability" as the delivery availability instead of the date (off for quotations made before this existed).
    delivery_subject_to_stock = models.BooleanField(default=False)
    payment_terms = models.CharField(max_length=60, blank=True)         # ticked options, comma separated (LC, PDC, Advance)
    payment_terms_note = models.CharField(max_length=300, blank=True)   # free text that clarifies the payment terms
    # Optional discount taken off the item total, before VAT: a fixed amount in AED, or a percentage of the total.
    discount_type = models.CharField(max_length=10, choices=[('amount', 'AED'), ('percent', '%')], default='amount')
    discount_value = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    country_of_origin = models.CharField(max_length=200, blank=True)
    warranty = models.CharField(max_length=300, blank=True)   # general warranty for the whole quotation (items have their own column too)
    sales_person = models.CharField(max_length=150, blank=True)
    email = models.CharField(max_length=200, blank=True)
    contact_number = models.CharField(max_length=100, blank=True)
    # Print the company stamp inside the bank details box (the image itself is uploaded once on the Stamp page).
    add_stamp = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    @classmethod
    def next_qtn_no(cls, year):
        """The next free number for `year`: JG-<year>-001, 002, ... (the count restarts every year).

        Numbers that don't follow the pattern (older, hand-typed ones) are ignored.
        """
        prefix = f'{QTN_PREFIX}-{year}-'
        last = 0
        for no in cls.objects.filter(qtn_no__startswith=prefix).values_list('qtn_no', flat=True):
            tail = no[len(prefix):]
            if tail.isdigit():
                last = max(last, int(tail))
        return f'{prefix}{last + 1:03d}'

    @property
    def total(self):
        """Sum of the item amounts, before VAT."""
        return sum((i.amount for i in self.items.all()), Decimal('0'))

    @property
    def discount_amount(self):
        """The discount in AED (never more than the total, never negative)."""
        value = max(self.discount_value or Decimal('0'), Decimal('0'))
        if self.discount_type == 'percent':
            value = (self.total * min(value, Decimal('100')) / 100).quantize(CENT, rounding=ROUND_HALF_UP)
        return min(value, self.total)

    @property
    def net_total(self):
        """Total amount excluding VAT: the item total less the discount."""
        return self.total - self.discount_amount

    @property
    def vat_amount(self):
        """5% VAT on the discounted total."""
        return (self.net_total * VAT_RATE).quantize(CENT, rounding=ROUND_HALF_UP)

    @property
    def grand_total(self):
        return self.net_total + self.vat_amount

    @property
    def last_email(self):
        """The most recent email attempt (uses prefetched `emails` when the list page loaded them)."""
        sent = list(self.emails.all())
        return max(sent, key=lambda m: (m.sent_at, m.pk)) if sent else None

    def __str__(self):
        return self.qtn_no


class QuotationEmail(models.Model):
    """One attempt to email a quotation: who sent it, to whom, when, and whether the mail server accepted it."""

    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name='emails')
    to_addrs = models.TextField()
    cc_addrs = models.TextField(blank=True)
    subject = models.CharField(max_length=300)
    orientation = models.CharField(max_length=10, default='portrait')
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    sent_at = models.DateTimeField(default=timezone.now)
    success = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    saved_to_sent = models.BooleanField(default=False)   # a copy was filed in the mailbox's Sent folder

    class Meta:
        ordering = ['-sent_at', '-id']


class QuotationStamp(models.Model):
    """Single row holding the company stamp image used on quotations (separate from every other app's stamp)."""

    # Private storage: outside MEDIA_ROOT, never served by URL.
    stamp = models.ImageField(upload_to=stamp_upload_to, storage=get_private_storage, blank=True, null=True)

    @classmethod
    def get_instance(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


def product_key(text):
    """How a Model No (or a file name) is compared: capitals, no outer spaces, '/' and '\\' as '-' (AB/12 = AB-12)."""
    return (text or '').strip().upper().replace('/', '-').replace('\\', '-')


def product_photo_upload_to(instance, filename):
    return f'library/photos/{secrets.token_hex(8)}_{filename}'


def product_datasheet_upload_to(instance, filename):
    return f'library/datasheets/{secrets.token_hex(8)}_{filename}'


class Customer(models.Model):
    """Customer list (imported from Excel or typed in): picking the name in a quotation fills these details.

    A quotation keeps its own copy of the details, so editing or deleting a customer here never changes one already made.
    """

    code = models.CharField(max_length=50)                                    # Customer ID, e.g. V00001
    code_key = models.CharField(max_length=50, unique=True, editable=False)   # code upper-cased: one customer per ID
    name = models.CharField(max_length=300)
    trn = models.CharField(max_length=50, blank=True)
    contact_number = models.CharField(max_length=200, blank=True)
    contact_person = models.CharField(max_length=200, blank=True)
    address = models.TextField(blank=True)
    email = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name', 'code_key']

    def save(self, *args, **kwargs):
        self.code = (self.code or '').strip()
        self.code_key = self.code.upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.code} {self.name}'


class Product(models.Model):
    """Product library: model no, brand, description, photo and datasheet entered once.

    Typing a Model No in a quotation copies these into the quotation row. The quotation keeps its own copy of the
    files, so changing or deleting a product here never alters a quotation that was already made or sent.
    """

    model_no = models.CharField(max_length=100)
    model_key = models.CharField(max_length=100, unique=True, editable=False)   # product_key(model_no): one product per model
    brand = models.CharField(max_length=150, blank=True)
    description = models.TextField(blank=True)
    photo = models.ImageField(upload_to=product_photo_upload_to, storage=get_private_storage, blank=True, null=True)
    datasheet = models.FileField(upload_to=product_datasheet_upload_to, storage=get_private_storage, blank=True, null=True)   # always a JPEG
    # File names given in the Excel "Photo file" / "Datasheet file" columns, used when the files are uploaded later.
    photo_name = models.CharField(max_length=200, blank=True)
    datasheet_name = models.CharField(max_length=200, blank=True)
    # Cost (from the stock data, admin only) and selling price (2 x cost). A value admin typed by hand is flagged *_manual and
    # is never overwritten by a stock refresh; see quotation/pricing.py.
    cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    selling_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    cost_manual = models.BooleanField(default=False)
    price_manual = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['model_key']

    def save(self, *args, **kwargs):
        self.model_no = (self.model_no or '').strip()
        self.model_key = product_key(self.model_no)
        if not self.price_manual and self.cost is not None:   # the automatic price always follows the cost
            self.selling_price = (self.cost * 2).quantize(Decimal('0.01'))
        super().save(*args, **kwargs)

    def __str__(self):
        return self.model_no


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
        return line_amount(self.qty, self.unit_price)
