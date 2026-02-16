from django import forms
from .models import SalesmanCard


class ExcelUploadForm(forms.Form):
    """Form for uploading Excel files"""
    
    excel_file = forms.FileField(
        label="Excel File",
        help_text="Upload an Excel file (.xlsx or .xls) with salesman data",
        widget=forms.FileInput(attrs={
            'class': 'form-control',
            'accept': '.xlsx,.xls'
        })
    )
    company_name = forms.CharField(
        label="Company Name",
        max_length=200,
        required=False,
        initial="Company Name",
        help_text="Default company name (can be overridden in Excel)",
        widget=forms.TextInput(attrs={
            'class': 'form-control'
        })
    )
    
    def clean_excel_file(self):
        """Validate Excel file"""
        file = self.cleaned_data.get('excel_file')
        if file:
            # Check file extension
            if not file.name.endswith(('.xlsx', '.xls')):
                raise forms.ValidationError("File must be an Excel file (.xlsx or .xls)")
            
            # Check file size (max 10MB)
            if file.size > 10 * 1024 * 1024:
                raise forms.ValidationError("File size must be less than 10MB")
        
        return file


class SalesmanCardForm(forms.ModelForm):
    """Form for manually creating/editing salesman cards"""
    
    class Meta:
        model = SalesmanCard
        fields = [
            'name', 'email', 'phone', 'designation', 'department',
            'company_name', 'photo', 'company_logo',
            'qr_foreground_color', 'qr_background_color', 'qr_size',
            'qr_embed_logo', 'qr_logo_size_percent',
            'qr_frame_style', 'qr_frame_text', 'qr_frame_text_color'
        ]
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Full name'
            }),
            'email': forms.EmailInput(attrs={
                'class': 'form-control',
                'placeholder': 'email@example.com'
            }),
            'phone': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '+1234567890'
            }),
            'designation': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Job title'
            }),
            'department': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Department name'
            }),
            'company_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Company Name'
            }),
            'photo': forms.FileInput(attrs={
                'class': 'form-control',
                'accept': 'image/*'
            }),
            'company_logo': forms.FileInput(attrs={
                'class': 'form-control',
                'accept': 'image/*'
            }),
            'qr_foreground_color': forms.TextInput(attrs={
                'class': 'form-control',
                'type': 'color',
                'style': 'width: 80px; height: 40px;'
            }),
            'qr_background_color': forms.TextInput(attrs={
                'class': 'form-control',
                'type': 'color',
                'style': 'width: 80px; height: 40px;'
            }),
            'qr_size': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '200',
                'max': '3000',
                'step': '50'
            }),
            'qr_embed_logo': forms.CheckboxInput(attrs={
                'class': 'form-check-input'
            }),
            'qr_logo_size_percent': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '10',
                'max': '40',
                'step': '5'
            }),
            'qr_frame_style': forms.Select(attrs={
                'class': 'form-select'
            }),
            'qr_frame_text': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g., Scan Me!'
            }),
            'qr_frame_text_color': forms.TextInput(attrs={
                'class': 'form-control',
                'type': 'color',
                'style': 'width: 80px; height: 40px;'
            }),
        }
        help_texts = {
            'photo': 'Optional: Upload a profile photo',
            'company_logo': 'Optional: Upload company logo',
            'qr_foreground_color': 'Color of QR code pattern',
            'qr_background_color': 'Background color of QR code',
            'qr_size': 'QR code size in pixels (200-3000)',
            'qr_embed_logo': 'Embed company logo in QR code center',
            'qr_logo_size_percent': 'Logo size as percentage of QR code (10-40%)',
            'qr_frame_style': 'Decorative frame style for QR code',
            'qr_frame_text': 'Optional text to display with frame',
            'qr_frame_text_color': 'Color for frame text',
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Make slug field readonly if editing
        if self.instance and self.instance.pk:
            self.fields['slug'] = forms.SlugField(
                required=False,
                widget=forms.TextInput(attrs={
                    'class': 'form-control',
                    'readonly': True
                }),
                help_text='URL-friendly identifier (auto-generated)'
            )
            self.fields['slug'].initial = self.instance.slug
    
    def clean_email(self):
        """Ensure email uniqueness"""
        email = self.cleaned_data.get('email')
        if email:
            # Check if another card with this email exists (excluding current instance)
            existing = SalesmanCard.objects.filter(email=email)
            if self.instance and self.instance.pk:
                existing = existing.exclude(pk=self.instance.pk)
            if existing.exists():
                raise forms.ValidationError("A card with this email already exists.")
        return email
