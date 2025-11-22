# in notifications/views.py
import pandas as pd
from django.shortcuts import render
from .forms import UploadFileForm
from .models import Customer
from datetime import datetime

def upload_file(request):
    if request.method == 'POST':
        form = UploadFileForm(request.POST, request.FILES)
        if form.is_valid():
            file = request.FILES['file']
            df = pd.read_excel(file)  # Or pd.read_csv(file)
            for index, row in df.iterrows():
                # Clean and parse the date
                expiry_date = None
                if pd.notna(row['Trade License Expiry']):
                    try:
                        # Attempt to parse different date formats
                        expiry_date = pd.to_datetime(row['Trade License Expiry'], errors='coerce').date()
                    except Exception as e:
                        print(f"Could not parse date: {row['Trade License Expiry']}, error: {e}")
                
                Customer.objects.update_or_create(
                    bp_code=row['BP Code'],
                    defaults={
                        'bp_name': row['BP Name'],
                        'sales_employee_code': row['Sales Employee Code'],
                        'sales_employee_name': row['Sales Employee Name'],
                        'trade_license_expiry': expiry_date,
                    }
                )
            return render(request, 'tradelicense/upload_success.html')
    else:
        form = UploadFileForm()
    return render(request, 'tradelicense/upload.html', {'form': form})