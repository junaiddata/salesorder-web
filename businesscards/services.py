"""
Services for business cards app:
- Excel import
- QR code generation
- vCard generation
"""
import os
import pandas as pd
import qrcode
from qrcode.image.pil import PilImage
from io import BytesIO
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.conf import settings
from django.utils.text import slugify
from .models import SalesmanCard


REQUIRED_COLUMNS = ['name', 'phone', 'email', 'designation', 'department']
OPTIONAL_COLUMNS = ['photo_filename', 'company_name']


def validate_excel_file(file):
    """
    Validate Excel file structure and return preview data.
    
    Returns:
        dict with keys:
            - valid: bool
            - errors: list of error messages
            - preview: list of dicts with row data (if valid)
            - total_rows: int
    """
    errors = []
    preview_data = []
    
    try:
        # Read Excel file
        df = pd.read_excel(file)
        
        # Check if file is empty
        if df.empty:
            errors.append("Excel file is empty")
            return {
                'valid': False,
                'errors': errors,
                'preview': [],
                'total_rows': 0
            }
        
        # Normalize column names (lowercase, strip whitespace)
        df.columns = df.columns.str.lower().str.strip()
        
        # Check required columns
        missing_columns = [col for col in REQUIRED_COLUMNS if col not in df.columns]
        if missing_columns:
            errors.append(f"Missing required columns: {', '.join(missing_columns)}")
        
        # If we have errors, return early
        if errors:
            return {
                'valid': False,
                'errors': errors,
                'preview': [],
                'total_rows': len(df)
            }
        
        # Convert to list of dicts for preview
        preview_data = df.head(10).to_dict('records')
        
        # Validate data types and required fields
        for idx, row in df.iterrows():
            row_num = idx + 2  # +2 because Excel is 1-indexed and has header
            if pd.isna(row.get('name')) or str(row.get('name')).strip() == '':
                errors.append(f"Row {row_num}: Name is required")
            if pd.isna(row.get('email')) or str(row.get('email')).strip() == '':
                errors.append(f"Row {row_num}: Email is required")
            if pd.isna(row.get('phone')) or str(row.get('phone')).strip() == '':
                errors.append(f"Row {row_num}: Phone is required")
            if pd.isna(row.get('designation')) or str(row.get('designation')).strip() == '':
                errors.append(f"Row {row_num}: Designation is required")
            if pd.isna(row.get('department')) or str(row.get('department')).strip() == '':
                errors.append(f"Row {row_num}: Department is required")
        
        return {
            'valid': len(errors) == 0,
            'errors': errors,
            'preview': preview_data,
            'total_rows': len(df),
            'columns': list(df.columns)
        }
        
    except Exception as e:
        errors.append(f"Error reading Excel file: {str(e)}")
        return {
            'valid': False,
            'errors': errors,
            'preview': [],
            'total_rows': 0
        }


@transaction.atomic
def import_excel_data(file, user, company_name=None, request=None):
    """
    Import salesman data from Excel file.
    
    Args:
        file: Excel file object
        user: User who is importing (for created_by field)
        company_name: Optional default company name
    
    Returns:
        dict with keys:
            - success: bool
            - created: int (number of new records)
            - updated: int (number of updated records)
            - errors: list of error messages
    """
    result = {
        'success': False,
        'created': 0,
        'updated': 0,
        'errors': []
    }
    
    try:
        # Validate file first
        validation = validate_excel_file(file)
        if not validation['valid']:
            result['errors'] = validation['errors']
            return result
        
        # Reset file pointer
        file.seek(0)
        df = pd.read_excel(file)
        df.columns = df.columns.str.lower().str.strip()
        
        # Process each row
        for idx, row in df.iterrows():
            try:
                # Get email (used as unique identifier for idempotent updates)
                email = str(row.get('email', '')).strip()
                if not email:
                    result['errors'].append(f"Row {idx + 2}: Email is required")
                    continue
                
                # Check if record exists
                card, created = SalesmanCard.objects.get_or_create(
                    email=email,
                    defaults={
                        'name': str(row.get('name', '')).strip(),
                        'phone': str(row.get('phone', '')).strip(),
                        'designation': str(row.get('designation', '')).strip(),
                        'department': str(row.get('department', '')).strip(),
                        'company_name': str(row.get('company_name', company_name or 'Company Name')).strip(),
                        'created_by': user,
                    }
                )
                
                if not created:
                    # Update existing record
                    card.name = str(row.get('name', '')).strip()
                    card.phone = str(row.get('phone', '')).strip()
                    card.designation = str(row.get('designation', '')).strip()
                    card.department = str(row.get('department', '')).strip()
                    if 'company_name' in row and pd.notna(row.get('company_name')):
                        card.company_name = str(row.get('company_name', '')).strip()
                    card.save()
                    result['updated'] += 1
                else:
                    result['created'] += 1
                
                # Generate QR code for this card
                try:
                    generate_qr_code(card, request=request)
                except Exception as qr_error:
                    # Log but don't fail the import if QR generation fails
                    result['errors'].append(f"Row {idx + 2}: QR code generation failed: {str(qr_error)}")
                
            except Exception as e:
                result['errors'].append(f"Row {idx + 2}: {str(e)}")
                continue
        
        result['success'] = True
        return result
        
    except Exception as e:
        result['errors'].append(f"Import error: {str(e)}")
        return result


def generate_qr_code(card, brand_color='#1B2A4A', request=None):
    """
    Generate QR code for a salesman card.
    
    Args:
        card: SalesmanCard instance
        brand_color: Hex color code for QR code (default: deep navy)
        request: Optional request object to build absolute URL
    
    Returns:
        str: Path to saved QR code file
    """
    # Generate card URL
    from django.urls import reverse
    card_url = reverse('businesscards:card_public', kwargs={'slug': card.slug})
    
    # Make it absolute URL
    if request:
        full_url = request.build_absolute_uri(card_url)
    else:
        # Try to get from Site framework
        try:
            from django.contrib.sites.models import Site
            current_site = Site.objects.get_current()
            protocol = 'https' if settings.DEBUG == False else 'http'
            full_url = f"{protocol}://{current_site.domain}{card_url}"
        except:
            # Fallback
            full_url = f"http://localhost:8000{card_url}"
    
    # Create QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(full_url)
    qr.make(fit=True)
    
    # Create image with brand color
    img = qr.make_image(fill_color=brand_color, back_color="white")
    
    # Save to media directory
    qr_dir = os.path.join(settings.MEDIA_ROOT, 'qr')
    os.makedirs(qr_dir, exist_ok=True)
    
    qr_filename = f'{card.slug}.png'
    qr_path = os.path.join(qr_dir, qr_filename)
    
    img.save(qr_path)
    
    return qr_path


def generate_vcard(card):
    """
    Generate vCard (.vcf) content for a salesman card.
    
    Args:
        card: SalesmanCard instance
    
    Returns:
        str: vCard content as string
    """
    vcard_lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"FN:{card.name}",
        f"ORG:{card.company_name}",
        f"TITLE:{card.designation}",
        f"TEL;TYPE=CELL:{card.phone}",
        f"EMAIL;TYPE=WORK:{card.email}",
    ]
    
    # Add department as a note
    if card.department:
        vcard_lines.append(f"NOTE:Department: {card.department}")
    
    # Add photo if available (base64 encoded)
    # Note: vCard 3.0 supports PHOTO but requires base64 encoding
    # For simplicity, we'll skip photo in vCard for now
    # TODO: Add photo support if needed
    
    vcard_lines.append("END:VCARD")
    
    return "\n".join(vcard_lines)
