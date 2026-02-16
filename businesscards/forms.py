from django import forms


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
