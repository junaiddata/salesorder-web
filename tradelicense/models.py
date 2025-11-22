# in notifications/models.py
from django.db import models

class Customer(models.Model):
    bp_code = models.CharField(max_length=20, unique=True)
    bp_name = models.CharField(max_length=255)
    sales_employee_code = models.IntegerField()
    sales_employee_name = models.CharField(max_length=255)
    trade_license_expiry = models.DateField(null=True, blank=True)

    def __str__(self):
        return self.bp_name

class Notification(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    message = models.TextField()
    sent_date = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, default='pending')

    def __str__(self):
        return f"Notification for {self.customer.bp_name}"