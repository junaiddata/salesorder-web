from django.db import models
from django.db import transaction
from django.contrib.auth.models import User
from PIL import Image
from io import BytesIO
from django.core.files.base import ContentFile
import os
from django.conf import settings

class Role(models.Model):
    ROLE_CHOICES = [
        ('Admin', 'Admin'),
        ('Salesman', 'Salesman'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)

    def __str__(self):
        return f"{self.user.username} - {self.role}"

        
# Create your models here.
class Items(models.Model):
    item_code = models.CharField(max_length=50, unique=True,db_index=True)
    item_description = models.CharField(max_length=100, db_index=True)
    item_upvc = models.CharField(max_length=50, blank=True, null=True, db_index=True)
    item_cost = models.FloatField(default=0.0)
    item_firm = models.CharField(max_length=100)

    item_price = models.FloatField(default=0.0)
    item_stock = models.IntegerField(default=0)
    class Meta:
        indexes = [
            models.Index(fields=['item_firm']),
        ]

    def __str__(self):
        return self.item_description

class IgnoreList(models.Model):
    item_code = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.item_code

class Customer(models.Model):
    customer_code = models.CharField(max_length=50, unique=True)
    customer_name = models.CharField(max_length=100)
    salesman = models.ForeignKey('Salesman', on_delete=models.CASCADE, null=True, blank=True)

    phone_number = models.CharField(max_length=50, blank=True, null=True)
    address = models.TextField(blank=True, null=True, help_text="Customer address from SAP API")
    vat_number = models.CharField(max_length=100, blank=True, null=True, help_text="VAT Number - Business Partner")

    #added fields
    month_pending_1 = models.FloatField(default=0.0)
    month_pending_2 = models.FloatField(default=0.0)
    month_pending_3 = models.FloatField(default=0.0)
    month_pending_4 = models.FloatField(default=0.0)
    month_pending_5 = models.FloatField(default=0.0)
    month_pending_6 = models.FloatField(default=0.0)
    old_months_pending = models.FloatField(default=0.0)
    credit_limit = models.FloatField(default=0.0)
    credit_days = models.CharField(default='0',max_length=30)
    total_outstanding = models.FloatField(default=0.0)
    pdc_received = models.FloatField(default=0.0)
    total_outstanding_with_pdc = models.FloatField(default=0.0)


    def __str__(self):
        return self.customer_name


class Salesman(models.Model):
    salesman_name = models.CharField(max_length=100)

    def __str__(self):
        return self.salesman_name

class SalesOrder(models.Model):
    STATUS = (
        ('Pending', 'Pending'),
        ('Hold by A/c', 'Hold by A/c'),
        ('Approved', 'Approved'),
        ('SO Created', 'SO Created'),
    )

    DIVISIONS = (
        ('JUNAID', 'Junaid'),
        ('ALABAMA', 'Alabama'),
    )

    division = models.CharField(max_length=20, choices=DIVISIONS, default='JUNAID')
    order_number = models.CharField(max_length=20, unique=True, blank=True)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    salesman = models.ForeignKey(Salesman, on_delete=models.SET_NULL, null=True, blank=True)
    # contact_number = models.CharField(max_length=100, blank=True, null=True)
    # delivery_address = models.TextField(blank=True, null=True)

    order_date = models.DateField(auto_now_add=True)
    order_date_time = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    total_amount = models.FloatField(default=0.0)
    order_taken = models.BooleanField(default=False)
    order_status = models.CharField(max_length=20, choices=STATUS, default='Pending')
    remarks = models.TextField(blank=True, null=True)
    salesman_remarks = models.CharField(max_length=255, blank=True, null=True)
    tax= models.FloatField(default=5.0)
    lpo_image = models.ImageField(upload_to='lpo_uploads/', null=True, blank=True)  # <-- added
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    location = models.CharField(max_length=100, blank=True, null=True)  # new field

    def __str__(self):
        return f"Order {self.id} - {self.customer.customer_name} " 

    def save(self, *args, **kwargs):
        # Generate order number if not exists
        if not self.order_number:
            if self.division == 'ALABAMA':
                prefix = "AL"
                # Filter specifically for Alabama orders to find the last number
                last_order = SalesOrder.objects.filter(order_number__startswith='AL').order_by('-id').first()
                start_number = 260001 # Start Alabama orders from 260001 or whatever you prefer
            else:
                prefix = "CO"
                # Filter specifically for Junaid orders
                last_order = SalesOrder.objects.filter(order_number__startswith='CO').order_by('-id').first()
                start_number = 260001

            if last_order and last_order.order_number:
                # Strip letters to get the number
                import re
                numbers = re.findall(r'\d+', last_order.order_number)
                if numbers:
                    last_number = int(numbers[0])
                    if last_number < start_number:
                        new_number = start_number
                    else:
                        new_number = last_number + 1
                else:
                    new_number = start_number
            else:
                new_number = start_number
            
            self.order_number = f"{prefix}{new_number}"

        # Process image only if it's being updated
        if self.lpo_image and hasattr(self.lpo_image, 'file'):
            from PIL import Image as PILImage
            from io import BytesIO
            import time

            try:
                # Get the original file content before Django modifies it
                original_file = self.lpo_image
                original_file.open('rb')
                file_content = original_file.read()
                original_file.close()

                with PILImage.open(BytesIO(file_content)) as img:
                    # Resize if needed
                    max_width = 1200
                    if img.width > max_width:
                        w_percent = (max_width / float(img.width))
                        h_size = int((float(img.height) * float(w_percent)))
                        img = img.resize((max_width, h_size), PILImage.Resampling.LANCZOS)

                    # Convert to buffer
                    buffer = BytesIO()
                    img_format = img.format.upper() if img.format else 'JPEG'
                    if img_format not in ['JPEG', 'PNG']:
                        img_format = 'JPEG'

                    img.save(buffer, format=img_format, quality=75, optimize=True)
                    buffer.seek(0)

                    # Generate deterministic filename
                    extension = 'jpg' if img_format == 'JPEG' else 'png'
                    file_name = f"{self.order_number}.{extension}"
                    
                    # Delete old file if exists (with retry logic)
                    if self.pk:
                        try:
                            old_obj = SalesOrder.objects.get(pk=self.pk)
                            if old_obj.lpo_image:
                                max_retries = 3
                                for i in range(max_retries):
                                    try:
                                        old_obj.lpo_image.delete(save=False)
                                        break
                                    except (PermissionError, OSError) as e:
                                        if i == max_retries - 1:
                                            raise
                                        time.sleep(0.1)  # Short delay before retry
                        except SalesOrder.DoesNotExist:
                            pass
                    
                    # Save new file with exact filename
                    self.lpo_image.save(
                        file_name, 
                        ContentFile(buffer.getvalue()), 
                        save=False
                    )
            
            except Exception as e:
                # Handle any errors during image processing
                raise ValueError(f"Error processing image: {str(e)}") from e

        # Single save operation
        super().save(*args, **kwargs)

    @property
    def status_badge(self):
        if self.order_taken:
            return '<span class="badge badge-success">Taken</span>'
        return '<span class="badge badge-warning">Pending</span>'

class CustomerPrice(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    item = models.ForeignKey(Items, on_delete=models.CASCADE)
    custom_price = models.FloatField(default=0.0)

    
    class Meta:
        unique_together = ('customer', 'item')

    def __str__(self):
        return f"{self.customer.customer_name} - {self.item.item_description}: {self.custom_price}"

class OrderItem(models.Model):
    unit_choices = [
        ('pcs', 'pcs'),
        ('ctn', 'ctn')
    ]
    order = models.ForeignKey(SalesOrder, related_name='items', on_delete=models.CASCADE)
    item = models.ForeignKey(Items, on_delete=models.CASCADE)
    quantity = models.IntegerField(default=1)
    unit = models.CharField(max_length=20, default='pcs')
    price = models.FloatField(default=0.0)
    is_custom_price = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.quantity} x {self.item.item_description} for {self.order.id}"


#################################################  Quotation Models #################################################
class Quotation(models.Model):

    DIVISIONS = (
        ('JUNAID', 'Junaid World'),
        ('ALABAMA', 'Alabama'),
    )

    quotation_number = models.CharField(max_length=20, unique=True, blank=True)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    salesman = models.ForeignKey(Salesman, on_delete=models.SET_NULL, null=True, blank=True)

    division = models.CharField(max_length=20, choices=DIVISIONS, default='JUNAID')
    quotation_date = models.DateField(auto_now_add=True)
    total_amount = models.FloatField(default=0.0)
    grand_total = models.FloatField(default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, default='Pending')
    remarks = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"Quotation {self.quotation_number} - {self.customer.customer_name}"

    def save(self, *args, **kwargs):
        # Generate quotation number if not exists
        if not self.quotation_number:
            # Logic for different prefixes based on division
            if self.division == 'ALABAMA':
                prefix = "ALQ"
            else:
                prefix = "QTN"

            last_quotation = Quotation.objects.filter(quotation_number__startswith=prefix).order_by('-id').first()
            
            if last_quotation and last_quotation.quotation_number:
                try:
                    # Strip prefix and get number
                    import re
                    numbers = re.findall(r'\d+', last_quotation.quotation_number)
                    if numbers:
                        new_number = int(numbers[0]) + 1
                    else:
                        new_number = 1001
                except (ValueError, IndexError):
                    new_number = 1001
            else:
                new_number = 1001
            
            self.quotation_number = f"{prefix}{new_number}"
        
        super().save(*args, **kwargs)

class QuotationItem(models.Model):
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name="items")
    item = models.ForeignKey(Items, on_delete=models.CASCADE, null=True)  # link to Items table
    unit = models.CharField(max_length=20, default='pcs')
    quantity = models.PositiveIntegerField()
    price = models.FloatField(default=0.0)
    line_total = models.FloatField(default=0.0)


    def __str__(self):
        return f"{self.item.item_description} ({self.quantity})"

# Quotation models
class SAPQuotation(models.Model):
    q_number = models.CharField(max_length=100, unique=True)  # Document Number
    internal_number = models.CharField(max_length=100, blank=True, null=True)
    posting_date = models.DateField(blank=True, null=True)
    customer_code = models.CharField(max_length=100, blank=True, null=True)
    customer_name = models.CharField(max_length=255)
    salesman_name = models.CharField(max_length=255, blank=True, null=True)
    brand = models.CharField(max_length=255, blank=True, null=True)
    bp_reference_no = models.CharField(max_length=255, blank=True, null=True)
    document_total = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    status = models.CharField(max_length=50, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    remarks = models.TextField(blank=True, null=True)
    bill_to = models.TextField(blank=True, null=True)  # 👈 added field

    def __str__(self):
        return f"{self.q_number} - {self.customer_name}"

    class Meta:
        indexes = [
            models.Index(fields=["posting_date"]),
            models.Index(fields=["salesman_name"]),
            models.Index(fields=["customer_name"]),
            models.Index(fields=["status"]),
        ]


class SAPQuotationItem(models.Model):
    quotation = models.ForeignKey(SAPQuotation, related_name='items', on_delete=models.CASCADE)
    item_no = models.CharField(max_length=100, blank=True, null=True)
    description = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    row_total = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)

    def __str__(self):
        return f"{self.quotation.q_number} - {self.description}"


# SAP Salesorder models
class SAPSalesorder(models.Model):
    so_number = models.CharField(max_length=100, unique=True)  # Document Number
    internal_number = models.CharField(max_length=100, blank=True, null=True)
    posting_date = models.DateField(blank=True, null=True)
    customer_code = models.CharField(max_length=100, blank=True, null=True)
    customer_name = models.CharField(max_length=255)
    salesman_name = models.CharField(max_length=255, blank=True, null=True)
    brand = models.CharField(max_length=255, blank=True, null=True)
    bp_reference_no = models.CharField(max_length=255, blank=True, null=True)
    vat_number = models.CharField(max_length=100, blank=True, null=True, help_text="VAT Number - Business Partner (from Excel upload)")
    discount_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, help_text="Discount percentage for the entire sales order")
    document_total = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    row_total_sum = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    status = models.CharField(max_length=50, blank=True, null=True)
    is_sap_pi = models.BooleanField(default=False, help_text="True if this SO has a Proforma Invoice created in SAP (U_PROFORMAINVOICE=Y)")
    customer_address = models.TextField(blank=True, null=True, help_text="Customer address from SAP API (Address field)")
    customer_phone = models.CharField(max_length=50, blank=True, null=True, help_text="Customer phone from SAP API (BusinessPartner.Phone1)")
    created_at = models.DateTimeField(auto_now_add=True)
    remarks = models.TextField(blank=True, null=True)
    bill_to = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.so_number} - {self.customer_name}"

    class Meta:
        indexes = [
            models.Index(fields=["posting_date"]),
            models.Index(fields=["salesman_name"]),
            models.Index(fields=["customer_name"]),
            models.Index(fields=["status"]),
        ]


class SAPSalesorderItem(models.Model):
    salesorder = models.ForeignKey(SAPSalesorder, related_name='items', on_delete=models.CASCADE)
    line_no = models.IntegerField(default=1, help_text="Line number within the sales order (1-based)")
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
    total_available_stock = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    dip_warehouse_stock = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=['salesorder', 'line_no']),
            models.Index(fields=['item_no']),  # For manufacturer lookup optimization
            models.Index(fields=['pending_amount']),  # For pending_total calculation
            models.Index(fields=['row_status']),  # For status filtering
        ]

    def __str__(self):
        return f"{self.salesorder.so_number} - {self.description}"


# SAP Proforma Invoice (PI) Models
class SAPProformaInvoice(models.Model):
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('CANCELLED', 'Cancelled'),
    ]

    salesorder = models.ForeignKey(SAPSalesorder, related_name='proforma_invoices', on_delete=models.CASCADE)
    pi_number = models.CharField(max_length=100, unique=True, help_text="Format: <SO_NUMBER>-P<N> for app PIs, <SO_NUMBER> for SAP PIs")
    sequence = models.IntegerField(help_text="Sequence number (1, 2, 3...) within the SO. For SAP PIs, use 0.")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    is_sap_pi = models.BooleanField(default=False, help_text="True if this PI was created in SAP (U_PROFORMAINVOICE=Y)")
    lpo_date = models.DateField(blank=True, null=True, help_text="LPO date for this Proforma Invoice")
    remarks = models.TextField(blank=True, null=True, help_text="Remarks/notes for the Proforma Invoice")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_pis')

    class Meta:
        indexes = [
            models.Index(fields=['salesorder', 'sequence']),
            models.Index(fields=['status']),
        ]
        ordering = ['salesorder', 'sequence']

    def __str__(self):
        return f"{self.pi_number} - {self.salesorder.so_number}"


class SAPProformaInvoiceLine(models.Model):
    pi = models.ForeignKey(SAPProformaInvoice, related_name='lines', on_delete=models.CASCADE)
    so_item = models.ForeignKey(SAPSalesorderItem, on_delete=models.SET_NULL, null=True, blank=True, help_text="Direct reference to SO item (for accurate allocation)")
    so_number = models.CharField(max_length=100, help_text="SO number for re-upload safety")
    line_no = models.IntegerField(help_text="Line number from SO item")
    # Snapshot fields (for display if SO line is deleted/re-uploaded)
    item_no = models.CharField(max_length=100, blank=True, null=True)
    description = models.CharField(max_length=255)
    manufacture = models.CharField(max_length=255, blank=True, null=True)
    job_type = models.CharField(max_length=255, blank=True, null=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        indexes = [
            models.Index(fields=['pi']),
            models.Index(fields=['so_number', 'line_no']),
            models.Index(fields=['so_item']),
        ]

    def __str__(self):
        return f"{self.pi.pi_number} - Line {self.line_no}: {self.description}"




################ LOGS #######################
from django.conf import settings
from django.db import models


import uuid
from django.conf import settings
from django.db import models

class Device(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='devices',
    )
    device_label = models.CharField(max_length=100, blank=True)  # "Rashad Laptop", “Office PC” (optional)
    user_agent = models.TextField(blank=True)
    device_type = models.CharField(max_length=20, blank=True)    # "PC", "Mobile", etc.
    device_os = models.CharField(max_length=100, blank=True)
    device_browser = models.CharField(max_length=100, blank=True)

    first_ip = models.GenericIPAddressField(null=True, blank=True)
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    last_seen = models.DateTimeField(auto_now=True)

    last_lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    last_lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.device_label or f"{self.user} - {self.device_type} - {self.device_browser}"

class QuotationLog(models.Model):
    ACTION_CHOICES = (
        ("created", "Created"),
        ("updated", "Updated"),
        ("deleted", "Deleted"),
    )

    quotation = models.ForeignKey(
        'Quotation', related_name='logs', on_delete=models.CASCADE
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='quotation_logs',
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

        # 🔽 NEW FIELDS
    location_lat = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    location_lng = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    network_label = models.CharField(
        max_length=100, blank=True
    )  # e.g. "DIP Office", "Home", "RAS Office"
        # 🔹 NEW: snapshot of device info at the time of action
    device_type = models.CharField(max_length=20, blank=True)      # "PC", "Mobile", "Tablet", ...
    device_os = models.CharField(max_length=100, blank=True)       # "Windows 10", "Android 14", ...
    device_browser = models.CharField(max_length=100, blank=True)  # "Chrome 125", "Edge 123", ...

    device = models.ForeignKey(
        Device,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='quotation_logs',
    )

    action = models.CharField(max_length=20, choices=ACTION_CHOICES, default="created")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.quotation.id} - {self.action} by {self.user or 'Anonymous'}"
    



class ProformaInvoiceLog(models.Model):
    ACTION_CHOICES = (
        ("created", "Created"),
        ("updated", "Updated"),
        ("cancelled", "Cancelled"),
    )

    pi = models.ForeignKey(
        'SAPProformaInvoice', related_name='logs', on_delete=models.CASCADE
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='pi_logs',
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    location_lat = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    location_lng = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    network_label = models.CharField(
        max_length=100, blank=True
    )
    device_type = models.CharField(max_length=20, blank=True)
    device_os = models.CharField(max_length=100, blank=True)
    device_browser = models.CharField(max_length=100, blank=True)

    device = models.ForeignKey(
        Device,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='pi_logs',
    )

    action = models.CharField(max_length=20, choices=ACTION_CHOICES, default="created")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.pi.pi_number} - {self.action} by {self.user or 'Anonymous'}"


class OpenSalesOrder(models.Model):
    # Mapping to Excel Columns
    document_no = models.CharField(max_length=50)               # "Document"
    posting_date = models.DateField(null=True, blank=True)      # "Posting Date"
    bp_reference = models.CharField(max_length=100, blank=True, null=True) # "BP Reference"
    customer_code = models.CharField(max_length=50, blank=True, null=True) # "Customer/Supplier Code"
    customer_name = models.CharField(max_length=255)            # "Customer/Supplier Name"
    item_no = models.CharField(max_length=50)                   # "Item No."
    description = models.CharField(max_length=255)              # "Item/Service Description"
    manufacturer = models.CharField(max_length=100, blank=True, null=True) # "Manufacturer"
    
    # Numeric Data
    quantity = models.FloatField(default=0.0)                   # "Quantity" (Total SO)
    row_total = models.FloatField(default=0.0)                  # "Row Total"
    open_qty = models.FloatField(default=0.0)                   # "Remaining" (Open Qty)
    total_available = models.FloatField(default=0.0)            # "Total avail"
    
    dip_stock = models.FloatField(default=0.0) 
    
    salesman_name = models.CharField(max_length=100, blank=True, null=True) # "Sales Employee"
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.document_no} - {self.customer_name}"

    class Meta:
        indexes = [
            models.Index(fields=['posting_date']),
            models.Index(fields=['salesman_name']),
            models.Index(fields=['manufacturer']),
        ]


from django.db import models
from django.contrib.auth.models import User
import uuid

class TrustedDevice(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    device_token = models.CharField(max_length=64, unique=True)
    device_name = models.CharField(max_length=100)
    user_agent = models.TextField(blank=True, null=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    # ✅ Add this field
    is_approved = models.BooleanField(default=False) 

    def __str__(self):
        status = "✅" if self.is_approved else "⏳"
        return f"{status} {self.user.username} - {self.device_name}"