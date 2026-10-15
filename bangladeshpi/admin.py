from django.contrib import admin

from .models import BangladeshPI, BangladeshPIItem


class ItemInline(admin.TabularInline):
    model = BangladeshPIItem
    extra = 0


@admin.register(BangladeshPI)
class BangladeshPIAdmin(admin.ModelAdmin):
    list_display = ('ref_suffix', 'date', 'created_at')
    inlines = [ItemInline]
