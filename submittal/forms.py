from django import forms


class TitlePageForm(forms.Form):
    """Step 1: Title page details."""
    project = forms.CharField(
        widget=forms.Textarea(attrs={
            'rows': 3, 'class': 'form-control',
            'placeholder': 'e.g. GEMS FPS – PHASE 2 (G+3 FIRST POINT SCHOOL)...'
        })
    )
    client = forms.CharField(
        max_length=255,
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
    product = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': 'e.g. COSMOPLAST – UPVC PIPES AND FITTINGS'
        })
    )


