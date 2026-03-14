from django.contrib import admin
from .models import (
    CompanyDocuments, SubmittalMaterial,
    MaterialCertification, ProjectContractorHistory, Submittal,
    SubmittalSectionUpload,
)


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
    list_display = ('model_no', 'item_description', 'material', 'brand', 'size', 'wras_number', 'pressure_rating', 'display_order')
    search_fields = ('model_no', 'item_description', 'material', 'brand')
    list_filter = ('brand',)
    inlines = [MaterialCertificationInline]


@admin.register(MaterialCertification)
class MaterialCertificationAdmin(admin.ModelAdmin):
    list_display = ('material', 'cert_type', 'description', 'uploaded_at')
    list_filter = ('cert_type',)
    search_fields = ('material__model_no', 'material__item_description', 'description')


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
