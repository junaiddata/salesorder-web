from django import forms

from .models import SubmittalBrand


class TitlePageForm(forms.Form):
    """Step 1: Title page details."""
    project = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            'rows': 3, 'class': 'form-control',
            'placeholder': 'e.g. GEMS FPS – PHASE 2 (G+3 FIRST POINT SCHOOL)...'
        })
    )
    client = forms.CharField(
        max_length=255, required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'e.g. M/s. PREMIER SCHOOL INTERNATIONAL'
        })
    )
    consultant = forms.CharField(
        max_length=255, required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'e.g. M/s. SUSTAINABLE ARCHITECTURAL & ENGINEERING'
        })
    )
    main_contractor = forms.CharField(
        max_length=255, required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'e.g. M/s. HESAL CONTRACTING. LLC'
        })
    )
    mep_contractor = forms.CharField(
        max_length=255, required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'e.g. M/s. HEAT AND POWER TECHNICAL SERVICES LLC'
        })
    )
    brand = forms.ModelChoiceField(
        queryset=SubmittalBrand.objects.all(),
        required=False,
        empty_label='— Select Brand —',
        widget=forms.Select(attrs={'class': 'form-control'}),
        label='Brand',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Keep brand list fresh (ordered) on every render.
        self.fields['brand'].queryset = SubmittalBrand.objects.order_by('display_order', 'name')


