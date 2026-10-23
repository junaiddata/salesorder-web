from django.contrib import admin

from .models import Customer, Product, Quotation, QuotationItem


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'updated_at')
    search_fields = ('code', 'name')


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('model_no', 'brand', 'updated_at')
    search_fields = ('model_no', 'brand')


class ItemInline(admin.TabularInline):
    model = QuotationItem
    extra = 0


@admin.register(Quotation)
class QuotationAdmin(admin.ModelAdmin):
    list_display = ('qtn_no', 'date', 'created_at')
    inlines = [ItemInline]
