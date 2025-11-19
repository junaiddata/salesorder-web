from django.contrib import admin
from .models import *
from django.utils.html import format_html
from django.contrib import admin
from .models import Quotation, QuotationLog


admin.site.register(Items)
admin.site.register(OrderItem)
admin.site.register(Salesman)
admin.site.register(CustomerPrice)
admin.site.register(SalesOrder)
admin.site.register(Role)
admin.site.register(IgnoreList)
# admin.site.register(Quotation)
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





from django.contrib import admin
from .models import Quotation, QuotationLog, Device  # adjust import if needed


class QuotationLogInline(admin.TabularInline):
    model = QuotationLog
    extra = 0
    ordering = ('-created_at',)

    readonly_fields = (
        'user',
        'device',
        'device_type',
        'device_os',
        'device_browser',
        'ip_address',
        'network_label',
        'location_lat',
        'location_lng',
        'user_agent',
        'action',
        'created_at',
    )


@admin.register(Quotation)
class QuotationAdmin(admin.ModelAdmin):
    inlines = [QuotationLogInline]
    list_display = ('id', 'customer', 'salesman', 'total_amount', 'grand_total')


class DeviceQuotationLogInline(admin.TabularInline):
    """
    Show recent logs for this device on its detail page.
    """
    model = QuotationLog
    extra = 0
    ordering = ('-created_at',)
    fields = (
        'quotation',
        'user',
        'ip_address',
        'network_label',
        'location_lat',
        'location_lng',
        'action',
        'created_at',
    )
    readonly_fields = fields


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    """
    Device Dashboard:
    One row per browser/device, with activity stats.
    """
    inlines = [DeviceQuotationLogInline]

    list_display = (
        'id_short',
        'user',
        'device_type',
        'device_browser',
        'first_ip',
        'last_ip',
        'last_lat',
        'last_lng',
        'last_seen',
        'quotation_count',
        'location_lat',      # 👈 use these names
        'location_lng',      # 👈 same names as in logs

    )

    list_filter = (
        'device_type',
        'device_os',
        'device_browser',
        'user',
        'is_active',
    )

    search_fields = (
        'id',
        'user__username',
        'user__first_name',
        'user__last_name',
        'first_ip',
        'last_ip',
        'device_os',
        'device_browser',
    )

    readonly_fields = (
        'id',
        'user',
        'device_label',
        'user_agent',
        'device_type',
        'device_os',
        'device_browser',
        'first_ip',
        'last_ip',
        'last_seen',
        'created_at',
        'is_active',
    )

    ordering = ('-last_seen',)

    # ------- helper columns -------

    def last_lat(self, obj): 
        return obj.last_lat

    def last_lng(self, obj): 
        return obj.last_lng
    def id_short(self, obj):
        return str(obj.id).split('-')[0]
    id_short.short_description = "Device ID"

    def quotation_count(self, obj):
        return obj.quotation_logs.count()
    quotation_count.short_description = "Quotations"

    def network_summary(self, obj):
        qs = obj.quotation_logs.exclude(network_label='')
        labels = list(qs.values_list('network_label', flat=True).distinct()[:3])
        if labels:
            text = ", ".join(labels)
            if qs.values('network_label').distinct().count() > 3:
                text += ", ..."
            return text
        return "-"
    network_summary.short_description = "Networks used"

    def location_lat(self, obj):
        """
        Latest latitude from QuotationLog for this device.
        """
        log = obj.quotation_logs.order_by('-created_at').first()
        return log.location_lat if log else None
    location_lat.short_description = "Lat"

    def location_lng(self, obj):
        """
        Latest longitude from QuotationLog for this device.
        """
        log = obj.quotation_logs.order_by('-created_at').first()
        return log.location_lng if log else None
    location_lng.short_description = "Lng"