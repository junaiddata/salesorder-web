from django.contrib import admin
from .models import SalesmanCard


@admin.action(description='Regenerate QR codes for selected cards')
def regenerate_qr_codes(modeladmin, request, queryset):
    """Admin action to regenerate QR codes"""
    from .services import generate_qr_code
    count = 0
    for card in queryset:
        try:
            generate_qr_code(card, request=request)
            count += 1
        except Exception as e:
            modeladmin.message_user(request, f"Error regenerating QR for {card.name}: {str(e)}", level='error')
    
    modeladmin.message_user(request, f"Successfully regenerated {count} QR codes.", level='success')


@admin.register(SalesmanCard)
class SalesmanCardAdmin(admin.ModelAdmin):
    list_display = ['name', 'email', 'phone', 'designation', 'department', 'slug', 'created_at']
    list_filter = ['department', 'designation', 'created_at']
    search_fields = ['name', 'email', 'phone', 'designation']
    readonly_fields = ['slug', 'created_at', 'updated_at']
    actions = [regenerate_qr_codes]
    
    fieldsets = (
        ('Personal Information', {
            'fields': ('name', 'email', 'phone', 'photo')
        }),
        ('Professional Information', {
            'fields': ('designation', 'department')
        }),
        ('Company Information', {
            'fields': ('company_name', 'company_logo')
        }),
        ('System Information', {
            'fields': ('slug', 'created_by', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
