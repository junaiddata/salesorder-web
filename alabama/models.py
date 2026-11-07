import secrets as _secrets

from django.conf import settings
from django.db import models

from so.item_media_storage import get_item_media_storage


class AlabamaItem(models.Model):
    """Alabama's own item master, loaded from the Alabama stock API (/api/alabama/stock?include_cost=1).

    Kept completely separate from so.Items (Junaid) so the two companies can have the same item code with a different
    brand / description / UPC / cost / stock. Field names deliberately mirror so.Items so display code that reads
    item_code / item_description / item_upvc / item_firm / item_cost / item_price / item_stock works on either.
    Values are stored exactly as the API sends them (cost included). Rows are never deleted by the sync: an item
    that disappears from the feed just stops being 'active' (see `is_active_in_feed`)."""

    SOURCE_FEED = 'feed'
    SOURCE_UPLOAD = 'upload'
    SOURCE_CHOICES = [(SOURCE_FEED, 'Alabama stock API'), (SOURCE_UPLOAD, 'Created by an Excel upload')]

    item_code = models.CharField(max_length=50, unique=True, db_index=True)
    item_description = models.CharField(max_length=500, blank=True, default='', db_index=True)
    item_upvc = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    item_firm = models.CharField(max_length=100, blank=True, default='')
    item_cost = models.FloatField(default=0.0)
    item_price = models.FloatField(default=0.0)
    item_stock = models.IntegerField(default=0)
    total_available_stock = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True,
                                                help_text='StockQuantity from the Alabama API')
    dip_warehouse_stock = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)

    quotable = models.BooleanField(default=False, db_index=True,
                                   help_text='Brand is on the Alabama brand list: offered when creating a quotation.')
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=SOURCE_FEED)
    last_seen_at = models.DateTimeField(null=True, blank=True, db_index=True,
                                        help_text='Time of the last sync run that listed this item.')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['item_code']
        indexes = [models.Index(fields=['item_firm'])]
        verbose_name = 'Alabama item'
        verbose_name_plural = 'Alabama items'

    def __str__(self):
        return f'{self.item_code} - {self.item_description}'


class AlabamaFeedBrand(models.Model):
    """A brand rule for the Alabama catalog, managed in the admin site. Items whose feed brand matches an ACTIVE rule
    are 'quotable' (offered on new Alabama quotations). Matching ignores case, spaces, hyphens and punctuation, so
    'ARISTON-CHINA' matches the feed's 'ARISTON - CHINA'. 'Starts with' rules ('RAKTHERM', 'GF') also pick up any
    brand added to the feed later whose name begins with that text."""

    MATCH_EXACT = 'exact'
    MATCH_PREFIX = 'prefix'
    MATCH_CHOICES = [(MATCH_EXACT, 'Exact name'), (MATCH_PREFIX, 'Starts with')]

    brand = models.CharField(max_length=100)
    match_type = models.CharField(max_length=10, choices=MATCH_CHOICES, default=MATCH_EXACT)
    is_active = models.BooleanField(default=True, help_text='Untick to stop offering this brand without deleting it.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['brand']
        unique_together = ('brand', 'match_type')
        verbose_name = 'Alabama feed brand'
        verbose_name_plural = 'Alabama feed brands'

    def __str__(self):
        return f'{self.brand} ({self.get_match_type_display()})'


class CatalogItemMixin:
    """`catalog_item`: the Alabama item when the line is linked to one, else the legacy so.Items row it was uploaded
    against. Lets Alabama screens read item_code / item_firm / ... without caring which one a line points at."""

    @property
    def catalog_item(self):
        from django.conf import settings
        if getattr(settings, 'ALABAMA_OWN_ITEMS', False):
            return self.alabama_item or self.item
        return self.item or self.alabama_item


class AlabamaSalesLine(CatalogItemMixin, models.Model):
    DOC_TYPE_CHOICES = [
        ('Invoice', 'Invoice'),
        ('Credit Memo', 'Credit Memo'),
    ]

    document_type = models.CharField(max_length=20, choices=DOC_TYPE_CHOICES)
    document_number = models.CharField(max_length=100, db_index=True)
    posting_date = models.DateField(db_index=True)

    # Customer Code → so.Customer (required FK)
    customer = models.ForeignKey(
        'so.Customer',
        on_delete=models.CASCADE,
        related_name='alabama_sales_lines',
    )

    # Denormalized for display (from Excel)
    sales_employee = models.CharField(max_length=255, blank=True, null=True)

    # Item Code → so.Items / Item Master (required FK)
    item = models.ForeignKey(
        'so.Items',
        on_delete=models.CASCADE,
        null=True, blank=True,   # optional: new lines may only have alabama_item (no row is created in Junaid's Items)
        related_name='alabama_sales_lines',
    )
    # Alabama's own item record (new). The legacy `item` link above is kept untouched as the fallback / rollback.
    alabama_item = models.ForeignKey(
        'alabama.AlabamaItem', on_delete=models.SET_NULL, null=True, blank=True, related_name='sales_lines',
    )

    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    gross_profit = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['document_type', 'document_number']),
            models.Index(fields=['posting_date']),
        ]

    def __str__(self):
        return f"{self.document_type} {self.document_number} - {self.customer.customer_name}"


class AlabamaSAPQuotation(models.Model):
    """Alabama SAP Quotation header - Excel upload."""
    q_number = models.CharField(max_length=100, unique=True, db_index=True)  # Document Number
    posting_date = models.DateField(blank=True, null=True, db_index=True)
    customer_code = models.CharField(max_length=100, blank=True, null=True)
    customer_name = models.CharField(max_length=255)
    salesman_name = models.CharField(max_length=255, blank=True, null=True)
    brand = models.CharField(max_length=255, blank=True, null=True)  # Manufacturer Name
    bp_reference_no = models.CharField(max_length=255, blank=True, null=True)
    document_total = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    status = models.CharField(max_length=50, blank=True, null=True)
    bill_to = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['posting_date']),
            models.Index(fields=['salesman_name']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"{self.q_number} - {self.customer_name}"


class AlabamaSalesmanMapping(models.Model):
    """Maps salesman name variants to canonical names (e.g. A.KADER -> KADER).
    Managed via Settings page so non-technical users can add mappings without code changes."""
    raw_name = models.CharField(max_length=255, help_text='Variant from Excel, e.g. A.KADER, A. KADER')
    normalized_name = models.CharField(max_length=255, help_text='Canonical name, e.g. KADER')

    class Meta:
        ordering = ['raw_name']

    def __str__(self):
        return f"{self.raw_name} → {self.normalized_name}"


class AlabamaDeliveryOrder(models.Model):
    """Delivery Order header - Excel upload. Uses salesman mappings for Sales Person."""
    do_number = models.CharField(max_length=100, unique=True, db_index=True, help_text='DO number')
    date = models.DateField(db_index=True)
    customer = models.ForeignKey(
        'so.Customer',
        on_delete=models.CASCADE,
        related_name='alabama_delivery_orders',
    )
    sales_person = models.CharField(max_length=255, blank=True, null=True)  # Normalized via Settings mappings
    city = models.CharField(max_length=255, blank=True, null=True)
    area = models.CharField(max_length=255, blank=True, null=True)
    lpo = models.CharField(max_length=255, blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)
    invoice = models.CharField(max_length=100, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-do_number']
        indexes = [
            models.Index(fields=['date']),
            models.Index(fields=['sales_person']),
        ]

    def __str__(self):
        return f"{self.do_number} - {self.customer.customer_name}"


class AlabamaDeliveryOrderItem(CatalogItemMixin, models.Model):
    """Delivery Order line item - Excel upload."""
    delivery_order = models.ForeignKey(
        AlabamaDeliveryOrder,
        related_name='items',
        on_delete=models.CASCADE,
    )
    item = models.ForeignKey(
        'so.Items',
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='alabama_delivery_order_items',
    )
    alabama_item = models.ForeignKey(
        'alabama.AlabamaItem', on_delete=models.SET_NULL, null=True, blank=True, related_name='delivery_order_lines',
    )
    item_description = models.CharField(max_length=500, blank=True, null=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def __str__(self):
        item = self.catalog_item
        return f"{self.delivery_order.do_number} - {item.item_code if item else ''}"


class AlabamaPurchaseLine(CatalogItemMixin, models.Model):
    """Purchase Summary line - Excel upload. DocumentTypeCode, Document Type, Document Number, Document Date,
    Vendor Code, Vendor Name, Sales Employee, ItemCode, ItemDescription, Quantity, UnitPrice, Item Manufacturer, Net Purchase."""
    document_type = models.CharField(max_length=100, db_index=True)  # e.g. Purchase Invoice, Purchase Credit Memo
    document_number = models.CharField(max_length=100, db_index=True)
    posting_date = models.DateField(db_index=True)

    vendor_code = models.CharField(max_length=100)
    vendor_name = models.CharField(max_length=255)

    sales_employee = models.CharField(max_length=255, blank=True, null=True)

    item = models.ForeignKey(
        'so.Items',
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='alabama_purchase_lines',
    )
    alabama_item = models.ForeignKey(
        'alabama.AlabamaItem', on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_lines',
    )
    item_description = models.CharField(max_length=500, blank=True, null=True)  # Denormalized from Excel
    item_manufacturer = models.CharField(max_length=255, blank=True, null=True)  # Denormalized from Excel

    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net_purchase = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['document_type', 'document_number']),
            models.Index(fields=['posting_date']),
        ]

    def __str__(self):
        return f"{self.document_type} {self.document_number} - {self.vendor_name}"


class AlabamaSAPQuotationItem(models.Model):
    """Alabama SAP Quotation line item - Excel upload."""
    quotation = models.ForeignKey(
        AlabamaSAPQuotation,
        related_name='items',
        on_delete=models.CASCADE,
    )
    item_no = models.CharField(max_length=100, blank=True, null=True)
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    row_total = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)

    def __str__(self):
        return f"{self.quotation.q_number} - {self.description}"


class AlabamaSalesOrder(models.Model):
    """Alabama Sales Order header - Excel upload. Mirror of SAPSalesorder (no approval/API fields)."""
    so_number = models.CharField(max_length=100, unique=True, db_index=True)
    posting_date = models.DateField(blank=True, null=True, db_index=True)
    customer_code = models.CharField(max_length=100, blank=True, null=True)
    customer_name = models.CharField(max_length=255)
    salesman_name = models.CharField(max_length=255, blank=True, null=True)
    bp_reference_no = models.CharField(max_length=255, blank=True, null=True)
    vat_number = models.CharField(max_length=100, blank=True, null=True)
    remarks = models.TextField(
        blank=True,
        null=True,
        help_text='Remarks from SAP / Excel upload (not editable as management remarks)',
    )
    management_remarks = models.TextField(
        blank=True,
        null=True,
        help_text='Internal management remarks; optional Telegram to salesman',
    )
    discount_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    document_total = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    row_total_sum = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    status = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['posting_date']),
            models.Index(fields=['salesman_name']),
            models.Index(fields=['customer_name']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"{self.so_number} - {self.customer_name}"


class AlabamaSalesOrderItem(models.Model):
    """Alabama Sales Order line item - Excel upload. Stock resolved from so.Items at display time."""
    salesorder = models.ForeignKey(
        AlabamaSalesOrder,
        related_name='items',
        on_delete=models.CASCADE,
    )
    line_no = models.IntegerField(default=1)
    item_no = models.CharField(max_length=100, blank=True, null=True)
    description = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    row_total = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    row_status = models.CharField(max_length=50, blank=True, null=True)
    job_type = models.CharField(max_length=255, blank=True, null=True)
    manufacture = models.CharField(max_length=255, blank=True, null=True)
    remaining_open_quantity = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    pending_amount = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=['salesorder', 'line_no']),
            models.Index(fields=['item_no']),
            models.Index(fields=['row_status']),
        ]

    def __str__(self):
        return f"{self.salesorder.so_number} - {self.description}"


# ---------------------------------------------------------------------------------------------------------------------
# Item Catalog photos / datasheets for Alabama items.
#
# Alabama normally REUSES the Junaid photo / datasheet of the same product (same item code AND same description, see
# alabama/media_match.py). These two tables only hold the exceptions, so nothing is copied or stored twice:
#   AlabamaItemMedia  - a photo / datasheet uploaded for an Alabama item (wins over a reused Junaid one)
#   AlabamaMediaLink  - an admin-approved link to a Junaid item whose code matches but whose description differs
# ---------------------------------------------------------------------------------------------------------------------
def alabama_photo_upload_to(instance, filename):
    return f'photos/al_{_secrets.token_hex(8)}.jpg'


def alabama_datasheet_upload_to(instance, filename):
    return f'datasheets/al_{_secrets.token_hex(8)}.jpg'


class AlabamaItemMedia(models.Model):
    alabama_item = models.OneToOneField(AlabamaItem, on_delete=models.CASCADE, related_name='media')
    photo = models.ImageField(upload_to=alabama_photo_upload_to, storage=get_item_media_storage, blank=True, null=True)
    datasheet = models.FileField(upload_to=alabama_datasheet_upload_to, storage=get_item_media_storage, blank=True, null=True)   # always a JPEG
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Alabama item photo / datasheet'
        verbose_name_plural = 'Alabama item photos / datasheets'

    def __str__(self):
        return f'Media for {self.alabama_item.item_code}'


class AlabamaMediaLink(models.Model):
    """Reuse the photo / datasheet of a Junaid item for an Alabama item although the descriptions differ (approved by an admin)."""
    alabama_item = models.OneToOneField(AlabamaItem, on_delete=models.CASCADE, related_name='media_link')
    junaid_item = models.ForeignKey('so.Items', on_delete=models.CASCADE, related_name='+')
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Alabama media link'
        verbose_name_plural = 'Alabama media links'

    def __str__(self):
        return f'{self.alabama_item.item_code} uses the files of Junaid {self.junaid_item.item_code}'
