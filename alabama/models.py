from django.db import models


class AlabamaSalesLine(models.Model):
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
        related_name='alabama_sales_lines',
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


class AlabamaDeliveryOrderItem(models.Model):
    """Delivery Order line item - Excel upload."""
    delivery_order = models.ForeignKey(
        AlabamaDeliveryOrder,
        related_name='items',
        on_delete=models.CASCADE,
    )
    item = models.ForeignKey(
        'so.Items',
        on_delete=models.CASCADE,
        related_name='alabama_delivery_order_items',
    )
    item_description = models.CharField(max_length=500, blank=True, null=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def __str__(self):
        return f"{self.delivery_order.do_number} - {self.item.item_code}"


class AlabamaPurchaseLine(models.Model):
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
        related_name='alabama_purchase_lines',
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
