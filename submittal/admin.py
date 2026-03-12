from django.contrib import admin
from .models import (
    CompanyDocuments, SectionDivider, SubmittalMaterial,
    MaterialCertification, ProjectContractorHistory, Submittal,
)


@admin.register(CompanyDocuments)
class CompanyDocumentsAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'updated_at')

    def has_add_permission(self, request):
        return not CompanyDocuments.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SectionDivider)
class SectionDividerAdmin(admin.ModelAdmin):
    list_display = ('section_num', 'section_name', 'divider_pdf')
    ordering = ('section_num',)


class MaterialCertificationInline(admin.TabularInline):
    model = MaterialCertification
    extra = 1


@admin.register(SubmittalMaterial)
class SubmittalMaterialAdmin(admin.ModelAdmin):
    list_display = ('item_code', 'description', 'brand', 'wras_number', 'display_order')
    search_fields = ('item_code', 'description', 'brand')
    list_filter = ('brand',)
    inlines = [MaterialCertificationInline]


@admin.register(MaterialCertification)
class MaterialCertificationAdmin(admin.ModelAdmin):
    list_display = ('material', 'cert_type', 'description', 'uploaded_at')
    list_filter = ('cert_type',)
    search_fields = ('material__item_code', 'material__description', 'description')


@admin.register(ProjectContractorHistory)
class ProjectContractorHistoryAdmin(admin.ModelAdmin):
    list_display = ('project', 'client', 'main_contractor', 'created_at')
    search_fields = ('project', 'client', 'main_contractor')


@admin.register(Submittal)
class SubmittalAdmin(admin.ModelAdmin):
    list_display = ('project', 'client', 'product', 'created_at')
    search_fields = ('project', 'client', 'product')
    filter_horizontal = ('materials',)
