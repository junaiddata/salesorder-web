from django import forms
from .models import Customer, Items

class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ['customer_code', 'customer_name', 'salesman']
        widgets = {
            'customer_code': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Unique customer code'
            }),
            'customer_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Customer name'
            }),
            'salesman': forms.Select(attrs={
                'class': 'form-select'
            })
        }

from django import forms
from .models import Items

class ItemForm(forms.ModelForm):
    class Meta:
        model = Items
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super(ItemForm, self).__init__(*args, **kwargs)
        
        # Get distinct firms from the Items table
        firms = Items.objects.values_list('item_firm', flat=True).distinct().order_by('item_firm')
        
        # Set the field as a dropdown
        self.fields['item_firm'] = forms.ChoiceField(
            choices=[(firm, firm) for firm in firms],
            required=True,
            label='Item Firm'
        )


from django import forms

class UploadFileForm(forms.Form):
    file = forms.FileField(label="Select Daily SO Excel File")