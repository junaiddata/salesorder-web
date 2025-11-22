from django.contrib import admin
from .models import Customer, Notification


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('bp_code', 'bp_name', 'sales_employee_code', 'sales_employee_name', 'trade_license_expiry')
    search_fields = ('bp_code', 'bp_name', 'sales_employee_name')
    list_filter = ('trade_license_expiry',)

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('customer', 'message', 'sent_date', 'status')
    search_fields = ('customer__bp_name', 'message', 'status')
    list_filter = ('status', 'sent_date')