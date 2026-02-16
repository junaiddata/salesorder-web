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
from PIL import Image, ImageDraw, ImageFont
import math
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


def generate_qr_code(card, request=None, size=None, foreground_color=None, background_color=None, embed_logo=None, logo_size_percent=None, frame_style=None, frame_text=None, frame_text_color=None):
    """
    Generate QR code for a salesman card with customization options.
    
    Args:
        card: SalesmanCard instance
        request: Optional request object to build absolute URL
        size: QR code size in pixels (overrides card setting)
        foreground_color: Foreground color hex (overrides card setting)
        background_color: Background color hex (overrides card setting)
        embed_logo: Whether to embed logo (overrides card setting)
        logo_size_percent: Logo size percentage (overrides card setting)
    
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
    
    # Get customization settings (use provided or card defaults)
    qr_size = size if size else card.qr_size
    fg_color = foreground_color if foreground_color else card.qr_foreground_color
    bg_color = background_color if background_color else card.qr_background_color
    embed = embed_logo if embed_logo is not None else card.qr_embed_logo
    logo_size = logo_size_percent if logo_size_percent else card.qr_logo_size_percent
    frame = frame_style if frame_style else card.qr_frame_style
    frame_txt = frame_text if frame_text is not None else card.qr_frame_text
    frame_txt_color = frame_text_color if frame_text_color else card.qr_frame_text_color
    
    # Calculate box_size based on desired output size
    # QR codes need error correction space, so we use higher error correction if embedding logo
    error_correction = qrcode.constants.ERROR_CORRECT_H if embed else qrcode.constants.ERROR_CORRECT_M
    border = 4
    
    # Estimate box_size (approximate calculation)
    # QR version determines data capacity, we'll let it auto-fit
    qr = qrcode.QRCode(
        version=None,  # Auto-determine version
        error_correction=error_correction,
        box_size=10,
        border=border,
    )
    qr.add_data(full_url)
    qr.make(fit=True)
    
    # Create QR code image
    qr_img = qr.make_image(fill_color=fg_color, back_color=bg_color)
    
    # Resize to desired size
    qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.LANCZOS)
    
    # Embed logo if requested and available
    if embed and card.company_logo:
        try:
            # Open logo
            logo_path = card.company_logo.path
            logo = Image.open(logo_path)
            
            # Convert to RGB if needed
            if logo.mode in ('RGBA', 'LA', 'P'):
                # Create white background
                bg = Image.new('RGB', logo.size, bg_color)
                if logo.mode == 'P':
                    logo = logo.convert('RGBA')
                bg.paste(logo, mask=logo.split()[-1] if logo.mode == 'RGBA' else None)
                logo = bg
            elif logo.mode != 'RGB':
                logo = logo.convert('RGB')
            
            # Calculate logo size (percentage of QR code)
            logo_dimension = int(qr_size * (logo_size / 100))
            logo_dimension = max(50, min(logo_dimension, int(qr_size * 0.4)))  # Clamp between 50px and 40% of QR
            
            # Resize logo maintaining aspect ratio
            logo.thumbnail((logo_dimension, logo_dimension), Image.Resampling.LANCZOS)
            
            # Create white background for logo (with padding)
            logo_bg_size = logo_dimension + 20
            logo_bg = Image.new('RGB', (logo_bg_size, logo_bg_size), bg_color)
            
            # Paste logo on white background (centered)
            logo_x = (logo_bg_size - logo.width) // 2
            logo_y = (logo_bg_size - logo.height) // 2
            logo_bg.paste(logo, (logo_x, logo_y))
            
            # Calculate position to paste logo (center of QR code)
            qr_center = qr_size // 2
            logo_x_pos = qr_center - logo_bg_size // 2
            logo_y_pos = qr_center - logo_bg_size // 2
            
            # Paste logo onto QR code
            qr_img.paste(logo_bg, (logo_x_pos, logo_y_pos))
            
        except Exception as e:
            # If logo embedding fails, continue without logo
            print(f"Warning: Could not embed logo in QR code: {str(e)}")
    
    # Apply frame if selected
    if frame and frame != 'none':
        qr_img = apply_frame_to_qr(qr_img, frame, frame_txt, frame_txt_color, fg_color if fg_color else '#000000')
    
    # Save to media directory
    qr_dir = os.path.join(settings.MEDIA_ROOT, 'qr')
    os.makedirs(qr_dir, exist_ok=True)
    
    qr_filename = f'{card.slug}.png'
    qr_path = os.path.join(qr_dir, qr_filename)
    
    # Save as PNG with high quality
    qr_img.save(qr_path, 'PNG', quality=95)
    
    return qr_path


def apply_frame_to_qr(qr_img, frame_style, frame_text=None, frame_text_color='#000000', border_color='#000000'):
    """
    Apply decorative frame to QR code image.
    
    Args:
        qr_img: PIL Image of QR code
        frame_style: Frame style name
        frame_text: Optional text to add (e.g., "Scan Me!")
        frame_text_color: Color for frame text
        border_color: Color for frame border
    
    Returns:
        PIL Image with frame applied
    """
    qr_width, qr_height = qr_img.size
    
    # Calculate padding based on frame style
    padding_map = {
        'simple': 20,
        'rounded': 30,
        'double': 40,
        'dashed': 30,
        'banner': 60,  # Extra space for banner text
        'badge': 50,
        'card': 40,
        'modern': 35,
        'classic': 45,
    }
    padding = padding_map.get(frame_style, 30)
    
    # Add extra padding if text is present
    if frame_text:
        padding += 40
    
    # Create new image with padding
    new_width = qr_width + (padding * 2)
    new_height = qr_height + (padding * 2)
    
    # Create background
    bg_img = Image.new('RGB', (new_width, new_height), 'white')
    
    # Paste QR code in center
    qr_x = padding
    qr_y = padding
    bg_img.paste(qr_img, (qr_x, qr_y))
    
    # Draw frame
    draw = ImageDraw.Draw(bg_img)
    
    # Convert hex color to RGB
    def hex_to_rgb(hex_color):
        hex_color = hex_color.lstrip('#')
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    
    border_rgb = hex_to_rgb(border_color)
    text_rgb = hex_to_rgb(frame_text_color)
    
    # Draw frame based on style
    if frame_style == 'simple':
        # Simple border
        draw.rectangle(
            [(padding - 5, padding - 5), (new_width - padding + 5, new_height - padding + 5)],
            outline=border_rgb,
            width=3
        )
    
    elif frame_style == 'rounded':
        # Rounded corners effect (simulated with multiple rectangles)
        corner_radius = 15
        # Top and bottom borders
        draw.rectangle(
            [(padding - 5, padding - 5 + corner_radius), (new_width - padding + 5, new_height - padding + 5 - corner_radius)],
            outline=border_rgb,
            width=3
        )
        # Left and right borders
        draw.rectangle(
            [(padding - 5 + corner_radius, padding - 5), (new_width - padding + 5 - corner_radius, new_height - padding + 5)],
            outline=border_rgb,
            width=3
        )
    
    elif frame_style == 'double':
        # Double border
        draw.rectangle(
            [(padding - 8, padding - 8), (new_width - padding + 8, new_height - padding + 8)],
            outline=border_rgb,
            width=2
        )
        draw.rectangle(
            [(padding - 3, padding - 3), (new_width - padding + 3, new_height - padding + 3)],
            outline=border_rgb,
            width=2
        )
    
    elif frame_style == 'dashed':
        # Dashed border
        dash_length = 10
        gap_length = 5
        # Top
        x = padding - 5
        while x < new_width - padding + 5:
            draw.line([(x, padding - 5), (min(x + dash_length, new_width - padding + 5), padding - 5)], fill=border_rgb, width=3)
            x += dash_length + gap_length
        # Bottom
        x = padding - 5
        while x < new_width - padding + 5:
            draw.line([(x, new_height - padding + 5), (min(x + dash_length, new_width - padding + 5), new_height - padding + 5)], fill=border_rgb, width=3)
            x += dash_length + gap_length
        # Left
        y = padding - 5
        while y < new_height - padding + 5:
            draw.line([(padding - 5, y), (padding - 5, min(y + dash_length, new_height - padding + 5))], fill=border_rgb, width=3)
            y += dash_length + gap_length
        # Right
        y = padding - 5
        while y < new_height - padding + 5:
            draw.line([(new_width - padding + 5, y), (new_width - padding + 5, min(y + dash_length, new_height - padding + 5))], fill=border_rgb, width=3)
            y += dash_length + gap_length
    
    elif frame_style == 'banner':
        # Banner style with text area at top
        banner_height = 40
        # Banner background
        draw.rectangle(
            [(padding - 5, padding - 5), (new_width - padding + 5, padding - 5 + banner_height)],
            fill=border_rgb
        )
        # Border around QR
        draw.rectangle(
            [(padding - 5, padding - 5 + banner_height), (new_width - padding + 5, new_height - padding + 5)],
            outline=border_rgb,
            width=3
        )
        # Add text if provided
        if frame_text:
            try:
                font = ImageFont.truetype("arial.ttf", 20)
            except:
                font = ImageFont.load_default()
            text_bbox = draw.textbbox((0, 0), frame_text, font=font)
            text_width = text_bbox[2] - text_bbox[0]
            text_x = (new_width - text_width) // 2
            text_y = padding - 5 + (banner_height - (text_bbox[3] - text_bbox[1])) // 2
            draw.text((text_x, text_y), frame_text, fill=(255, 255, 255), font=font)
    
    elif frame_style == 'badge':
        # Badge style with rounded top
        badge_height = 35
        # Badge top (semi-circle effect)
        draw.ellipse(
            [(new_width // 2 - badge_height // 2, padding - 5), (new_width // 2 + badge_height // 2, padding - 5 + badge_height)],
            outline=border_rgb,
            width=3
        )
        # Border around QR
        draw.rectangle(
            [(padding - 5, padding - 5 + badge_height // 2), (new_width - padding + 5, new_height - padding + 5)],
            outline=border_rgb,
            width=3
        )
        # Add text if provided
        if frame_text:
            try:
                font = ImageFont.truetype("arial.ttf", 18)
            except:
                font = ImageFont.load_default()
            text_bbox = draw.textbbox((0, 0), frame_text, font=font)
            text_width = text_bbox[2] - text_bbox[0]
            text_x = (new_width - text_width) // 2
            text_y = padding - 5 + badge_height // 4
            draw.text((text_x, text_y), frame_text, fill=text_rgb, font=font)
    
    elif frame_style == 'card':
        # Card style with shadow effect
        # Shadow
        shadow_offset = 5
        draw.rectangle(
            [(padding - 5 + shadow_offset, padding - 5 + shadow_offset), 
             (new_width - padding + 5 + shadow_offset, new_height - padding + 5 + shadow_offset)],
            fill=(200, 200, 200)
        )
        # Card border
        draw.rectangle(
            [(padding - 5, padding - 5), (new_width - padding + 5, new_height - padding + 5)],
            outline=border_rgb,
            width=3,
            fill='white'
        )
    
    elif frame_style == 'modern':
        # Modern style with accent lines
        accent_width = 4
        # Top accent
        draw.rectangle(
            [(padding - 5, padding - 5), (new_width - padding + 5, padding - 5 + accent_width)],
            fill=border_rgb
        )
        # Bottom accent
        draw.rectangle(
            [(padding - 5, new_height - padding + 5 - accent_width), (new_width - padding + 5, new_height - padding + 5)],
            fill=border_rgb
        )
        # Side borders
        draw.rectangle(
            [(padding - 5, padding - 5), (new_width - padding + 5, new_height - padding + 5)],
            outline=border_rgb,
            width=2
        )
    
    elif frame_style == 'classic':
        # Classic ornate frame
        # Outer border
        draw.rectangle(
            [(padding - 8, padding - 8), (new_width - padding + 8, new_height - padding + 8)],
            outline=border_rgb,
            width=2
        )
        # Inner border
        draw.rectangle(
            [(padding - 3, padding - 3), (new_width - padding + 3, new_height - padding + 3)],
            outline=border_rgb,
            width=2
        )
        # Corner decorations
        corner_size = 15
        # Top-left corner
        draw.line([(padding - 8, padding - 8), (padding - 8 + corner_size, padding - 8)], fill=border_rgb, width=2)
        draw.line([(padding - 8, padding - 8), (padding - 8, padding - 8 + corner_size)], fill=border_rgb, width=2)
        # Top-right corner
        draw.line([(new_width - padding + 8, padding - 8), (new_width - padding + 8 - corner_size, padding - 8)], fill=border_rgb, width=2)
        draw.line([(new_width - padding + 8, padding - 8), (new_width - padding + 8, padding - 8 + corner_size)], fill=border_rgb, width=2)
        # Bottom-left corner
        draw.line([(padding - 8, new_height - padding + 8), (padding - 8 + corner_size, new_height - padding + 8)], fill=border_rgb, width=2)
        draw.line([(padding - 8, new_height - padding + 8), (padding - 8, new_height - padding + 8 - corner_size)], fill=border_rgb, width=2)
        # Bottom-right corner
        draw.line([(new_width - padding + 8, new_height - padding + 8), (new_width - padding + 8 - corner_size, new_height - padding + 8)], fill=border_rgb, width=2)
        draw.line([(new_width - padding + 8, new_height - padding + 8), (new_width - padding + 8, new_height - padding + 8 - corner_size)], fill=border_rgb, width=2)
    
    # Add text below QR if provided and not in banner/badge
    if frame_text and frame_style not in ['banner', 'badge']:
        try:
            font = ImageFont.truetype("arial.ttf", 24)
        except:
            font = ImageFont.load_default()
        text_bbox = draw.textbbox((0, 0), frame_text, font=font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]
        text_x = (new_width - text_width) // 2
        text_y = new_height - padding + 10
        draw.text((text_x, text_y), frame_text, fill=text_rgb, font=font)
    
    return bg_img


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
