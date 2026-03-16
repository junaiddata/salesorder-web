from django.contrib import admin
from .models import (
    CompanyDocuments, SubmittalBrand, SubmittalMaterial,
    MaterialCertification, ProjectContractorHistory, Submittal,
    SubmittalSectionUpload,
)


@admin.register(SubmittalBrand)
class SubmittalBrandAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'display_order')
    ordering = ('display_order', 'name')


@admin.register(CompanyDocuments)
class CompanyDocumentsAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'updated_at')

    def has_add_permission(self, request):
        return not CompanyDocuments.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


class MaterialCertificationInline(admin.TabularInline):
    model = MaterialCertification
    extra = 1


@admin.register(SubmittalMaterial)
class SubmittalMaterialAdmin(admin.ModelAdmin):
    list_display = ('model_no', '_item_desc', 'brand', '_material', 'display_order')
    search_fields = ('model_no', 'data')
    list_filter = ('brand',)
    inlines = [MaterialCertificationInline]

    def _item_desc(self, obj):
        return obj.get('item_description') or '-'
    _item_desc.short_description = 'Item Description'

    def _material(self, obj):
        return obj.get('material') or '-'
    _material.short_description = 'Material'


@admin.register(MaterialCertification)
class MaterialCertificationAdmin(admin.ModelAdmin):
    list_display = ('material', 'cert_type', 'description', 'uploaded_at')
    list_filter = ('cert_type',)
    search_fields = ('material__model_no', 'description')


@admin.register(ProjectContractorHistory)
class ProjectContractorHistoryAdmin(admin.ModelAdmin):
    list_display = ('project', 'client', 'main_contractor', 'created_at')
    search_fields = ('project', 'client', 'main_contractor')


class SubmittalSectionUploadInline(admin.TabularInline):
    model = SubmittalSectionUpload
    extra = 0


@admin.register(Submittal)
class SubmittalAdmin(admin.ModelAdmin):
    list_display = ('project', 'client', 'product', 'created_at')
    search_fields = ('project', 'client', 'product')
    filter_horizontal = ('materials',)
    inlines = [SubmittalSectionUploadInline]
