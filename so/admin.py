from django.contrib import admin
from .models import *
from django.utils.html import format_html

admin.site.register(Items)
admin.site.register(OrderItem)
admin.site.register(Salesman)
admin.site.register(CustomerPrice)
admin.site.register(SalesOrder)
admin.site.register(Role)
admin.site.register(IgnoreList)
admin.site.register(Quotation)
admin.site.register(QuotationItem)
admin.site.register(SAPQuotation)
admin.site.register(SAPQuotationItem)




@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    # Display fields in list view
    list_display = ('customer_code', 'customer_name', 'salesman')
    list_display_links = ('customer_code', 'customer_name')
    
    # Add search functionality
    search_fields = ('customer_code', 'customer_name', 'phone_number')
    
    # Add filters
    list_filter = ('salesman','phone_number')  # Add created_at to your model if needed
    
    # Fields in edit/create form
    fieldsets = (
        ('Basic Info', {
            'fields': ('customer_code', 'customer_name')
        }),
        ('Sales Info', {
            'fields': ('salesman', 'credit_limit', 'credit_days'),
        }),
    )