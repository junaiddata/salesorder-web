from django import forms
from django.contrib import admin, messages
from django.db.models import Count
from django.utils.html import format_html, format_html_join

from .item_sync import norm_brand, refresh_quotable_flags
from .models import AlabamaFeedBrand, AlabamaItem


def alabama_brand_counts():
    """[(brand, item count)] for every brand in Alabama's item master (the API feed), A-Z."""
    return list(AlabamaItem.objects.exclude(item_firm='').values('item_firm').annotate(n=Count('id'))
                .order_by('item_firm').values_list('item_firm', 'n'))


class BrandSuggestInput(forms.TextInput):
    """A text box with a dropdown of every Alabama brand (with its item count). It still accepts typed text, so a
    'Starts with' rule such as GF or RAKTHERM (not one exact brand) can be entered."""

    def render(self, name, value, attrs=None, renderer=None):
        attrs = dict(attrs or {}, list=f'{name}-suggestions', autocomplete='off', size=50)
        box = super().render(name, value, attrs, renderer)
        options = format_html_join('', '<option value="{}" label="{} items">', alabama_brand_counts())
        return format_html('{}<datalist id="{}-suggestions">{}</datalist>', box, name, options)


class AlabamaFeedBrandForm(forms.ModelForm):
    class Meta:
        model = AlabamaFeedBrand
        fields = ['brand', 'match_type', 'is_active']
        widgets = {'brand': BrandSuggestInput}
        help_texts = {'brand': 'Pick a brand from the dropdown list (all brands of the Alabama API) or type a name. '
                               'For "Starts with" type just the beginning, e.g. RAKTHERM or GF.'}

    def clean_brand(self):
        brand = (self.cleaned_data.get('brand') or '').strip()
        match_type = self.data.get('match_type') or AlabamaFeedBrand.MATCH_EXACT
        clash = [b for b in AlabamaFeedBrand.objects.filter(match_type=match_type).exclude(pk=self.instance.pk)
                 if norm_brand(b.brand) == norm_brand(brand)]
        if clash:
            raise forms.ValidationError('This brand is already in the list.')
        return brand


@admin.register(AlabamaFeedBrand)
class AlabamaFeedBrandAdmin(admin.ModelAdmin):
    """Brand list for the Alabama catalog. Saving re-applies it to every Alabama item straight away."""
    form = AlabamaFeedBrandForm
    list_display = ('brand', 'match_type', 'is_active', 'items_matched', 'created_at')
    list_filter = ('match_type', 'is_active')
    search_fields = ('brand',)

    @admin.display(description='Items in the Alabama API matching this rule')
    def items_matched(self, obj):
        wanted = norm_brand(obj.brand)
        if not wanted:
            return 0
        total = 0
        for brand, n in alabama_brand_counts():
            nb = norm_brand(brand)
            if (nb.startswith(wanted) if obj.match_type == AlabamaFeedBrand.MATCH_PREFIX else nb == wanted):
                total += n
        return total

    def _refresh(self, request):
        changed = refresh_quotable_flags()
        self.message_user(request, f'Alabama brand list applied: {changed} items changed.', messages.INFO)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not self.items_matched(obj):
            self.message_user(
                request,
                f'"{obj.brand}" does not match any brand in the Alabama API yet, so no items are offered for it. '
                'Check the spelling, or ignore this if the brand will be added to the API later.',
                messages.WARNING)
        self._refresh(request)

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        self._refresh(request)

    def delete_queryset(self, request, queryset):
        super().delete_queryset(request, queryset)
        self._refresh(request)


@admin.register(AlabamaItem)
class AlabamaItemAdmin(admin.ModelAdmin):
    list_display = ('item_code', 'item_description', 'item_firm', 'item_cost', 'item_price', 'item_stock', 'quotable', 'source', 'last_seen_at')
    list_filter = ('quotable', 'source', 'item_firm')
    search_fields = ('item_code', 'item_description', 'item_upvc', 'item_firm')
    readonly_fields = ('created_at', 'updated_at')
