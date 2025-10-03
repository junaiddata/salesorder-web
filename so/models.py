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
    item_code = models.CharField(max_length=50, unique=True)
    item_description = models.CharField(max_length=100)
    item_upvc = models.CharField(max_length=50, blank=True, null=True)
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

    phone_number = models.CharField(max_length=15, blank=True, null=True)

    #added fields
    month_pending_1 = models.FloatField(default=0.0)
    month_pending_2 = models.FloatField(default=0.0)
    month_pending_3 = models.FloatField(default=0.0)
    month_pending_4 = models.FloatField(default=0.0)
    month_pending_5 = models.FloatField(default=0.0)
    month_pending_6 = models.FloatField(default=0.0)
    old_months_pending = models.FloatField(default=0.0)
    credit_limit = models.FloatField(default=0.0)
    credit_days = models.CharField(default='0',max_length=10)
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
    tax= models.FloatField(default=5.0)
    lpo_image = models.ImageField(upload_to='lpo_uploads/', null=True, blank=True)  # <-- added
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    location = models.CharField(max_length=100, blank=True, null=True)  # new field

    def __str__(self):
        return f"Order {self.id} - {self.customer.customer_name} " 

    def save(self, *args, **kwargs):
        # Generate order number if not exists
        if not self.order_number:
            last_order = SalesOrder.objects.order_by('-id').first()
            if last_order and last_order.order_number:
                last_number = int(last_order.order_number[2:])
                new_number = last_number + 1
            else:
                new_number = 250001
            self.order_number = f"CO{new_number}"

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
    quotation_number = models.CharField(max_length=20, unique=True, blank=True)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    salesman = models.ForeignKey(Salesman, on_delete=models.SET_NULL, null=True, blank=True)
    quotation_date = models.DateField(auto_now_add=True)
    total_amount = models.FloatField(default=0.0)
    grand_total = models.FloatField(default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, default='Pending')

    def __str__(self):
        return f"Quotation {self.quotation_number} - {self.customer.customer_name}"

    def save(self, *args, **kwargs):
        # Generate quotation number if not exists
        if not self.quotation_number:
            last_quotation = Quotation.objects.order_by('-id').first()
            if last_quotation and last_quotation.quotation_number:
                try:
                    last_number = int(last_quotation.quotation_number[3:])  # Remove "QTN" prefix
                    new_number = last_number + 1
                except (ValueError, IndexError):
                    new_number = 1001
            else:
                new_number = 1001
            self.quotation_number = f"QTN{new_number}"
        
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

