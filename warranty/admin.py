from django.contrib import admin

from .models import WarrantyBrandTerms, WarrantyLetterSettings, WarrantyLetter, WarrantyLetterItem


@admin.register(WarrantyLetterSettings)
class WarrantyLetterSettingsAdmin(admin.ModelAdmin):
    list_display = ('company', 'legal_company_name', 'ref_no_prefix', 'updated_at')
    # unique=True on `company` already caps this at one row per company
    # (junaid/alabama) -- no has_add_permission override needed.


@admin.register(WarrantyBrandTerms)
class WarrantyBrandTermsAdmin(admin.ModelAdmin):
    list_display = ('brand', 'updated_at')
    search_fields = ('brand',)


class WarrantyLetterItemInline(admin.TabularInline):
    model = WarrantyLetterItem
    extra = 0


@admin.register(WarrantyLetter)
class WarrantyLetterAdmin(admin.ModelAdmin):
    list_display = ('ref_no', 'project', 'client', 'company', 'status', 'created_at')
    list_filter = ('company', 'status')
    search_fields = ('ref_no', 'project', 'client')
    inlines = [WarrantyLetterItemInline]
    readonly_fields = ('created_by', 'created_at', 'updated_at', 'generated_pdf', 'pdf_generated_at')
