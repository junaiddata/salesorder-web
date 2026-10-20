from django.contrib import admin

from .models import Quotation, QuotationItem


class ItemInline(admin.TabularInline):
    model = QuotationItem
    extra = 0


@admin.register(Quotation)
class QuotationAdmin(admin.ModelAdmin):
    list_display = ('qtn_no', 'date', 'created_at')
    inlines = [ItemInline]
