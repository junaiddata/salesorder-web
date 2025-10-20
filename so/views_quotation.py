from django.views.decorators.csrf import csrf_exempt
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from .models import Quotation, QuotationItem, Customer, Salesman, Items, CustomerPrice


@csrf_exempt
@transaction.atomic
def create_quotation(request):
    if request.method == 'POST':
        try:
            # -------------------------
            # Customer handling - FIXED
            # -------------------------
            customer_id = request.POST.get('customer')
            firms = Items.objects.values_list('item_firm', flat=True).distinct().order_by('item_firm')
            
            new_customer_name = request.POST.get('new_customer_name', '').strip()

            if not customer_id and not new_customer_name:
                messages.error(request, 'Please select a customer or enter a new customer name.')
                return redirect('create_quotation')

            # Get salesman FIRST (required for both new and existing customers)
            salesman_id = request.POST.get('salesman')
            if not salesman_id:
                messages.error(request, 'Salesman is required.')
                return redirect('create_quotation')
                
            salesman = get_object_or_404(Salesman, id=salesman_id)

            if new_customer_name:
                # Generate customer code like in sales order
                last_customer = Customer.objects.filter(customer_code__startswith='NEWCUSTOMER') \
                                                .order_by('-id').first()
                if last_customer and last_customer.customer_code[11:].isdigit():
                    last_number = int(last_customer.customer_code[11:])
                else:
                    last_number = 0

                new_code = f'NEWCUSTOMERS{last_number + 1}'

                # Create new customer
                customer, created = Customer.objects.get_or_create(
                    customer_name=new_customer_name,
                    defaults={
                        'customer_code': new_code,
                        'salesman': salesman
                    }
                )
                if not created:
                    messages.error(request, f'Customer "{new_customer_name}" already exists.')
                    return redirect('create_quotation')
            else:
                customer = get_object_or_404(Customer, id=customer_id)

            # -------------------------
            # Items validation
            # -------------------------
            item_ids = request.POST.getlist('item')
            quantities = request.POST.getlist('quantity')
            prices = request.POST.getlist('price')
            units = request.POST.getlist('unit')

            if item_ids:  # POST with items
                if len(item_ids) != len(quantities) or len(item_ids) != len(prices) or len(item_ids) != len(units):
                    messages.error(request, 'Invalid form data. Please try again.')
                    return redirect('create_quotation')

                # -------------------------
                # Create Quotation
                # -------------------------
                quotation = Quotation.objects.create(
                    customer=customer,
                    salesman=salesman
                )

                quotation_items = []
                customer_price_updates = []
                total_amount = 0

                for i, (item_id, qty, price_input, unit) in enumerate(zip(item_ids, quantities, prices, units)):
                    try:
                        item = Items.objects.get(id=item_id)
                        quantity_val = int(qty)
                        unit_val = unit if unit in ['pcs', 'ctn','roll'] else 'pcs'

                        # Automatic price from CustomerPrice or default item price
                        customer_price = CustomerPrice.objects.filter(customer=customer, item=item).first()
                        price_val = float(price_input) if price_input else (customer_price.custom_price if customer_price else float(item.item_price))

                        if quantity_val <= 0:
                            messages.error(request, f'Quantity must be positive for item {i+1}.')
                            return redirect('create_quotation')
                        if price_val < 0:
                            messages.error(request, f'Price cannot be negative for item {i+1}.')
                            return redirect('create_quotation')

                        line_total = quantity_val * price_val
                        total_amount += line_total

                        quotation_items.append(QuotationItem(
                            quotation=quotation,
                            item=item,
                            quantity=quantity_val,
                            price=price_val,
                            unit=unit_val,
                            line_total=line_total
                        ))

                        # CustomerPrice update if new price entered
                        if price_input:
                            customer_price_updates.append((customer, item, price_val))

                    except (ValueError, Items.DoesNotExist) as e:
                        messages.error(request, f'Invalid data for item {i+1}: {str(e)}')
                        return redirect('create_quotation')

                # Bulk insert quotation items
                QuotationItem.objects.bulk_create(quotation_items)

                # Update CustomerPrice
                for c, it, pr in customer_price_updates:
                    CustomerPrice.objects.update_or_create(
                        customer=c,
                        item=it,
                        defaults={'custom_price': pr}
                    )

                # Save totals
                quotation.total_amount = total_amount
                quotation.grand_total = total_amount
                quotation.save()

                messages.success(request, 'Quotation created successfully!')
                return redirect('view_quotations')

            # If GET or POST without items, just render form
            customers = Customer.objects.all()
            salesmen = Salesman.objects.all()
            items = Items.objects.all()
            return render(request, 'orders/create_quotation.html', {
                'customers': customers,
                'salesmen': salesmen,
                'items': items,
                'firms': firms,
            })

        except Exception as e:
            messages.error(request, f'An error occurred: {str(e)}')
            return redirect('create_quotation')

    else:
        # GET request → render empty form
        customers = Customer.objects.all()
        salesmen = Salesman.objects.all()
        items = Items.objects.all()
        firms = Items.objects.values_list('item_firm', flat=True).distinct().order_by('item_firm')
        return render(request, 'so/quotations/create_quotation.html', {
            'customers': customers,
            'salesmen': salesmen,
            'firms': firms,
            'items': items,
        })

from django.shortcuts import render
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from .models import Quotation, Salesman

def view_quotations(request):
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    salesman_filter = request.GET.get('salesman_filter')
    page = request.GET.get('page', 1)
    status = request.GET.get('status', 'All')  # Default to 'All'

    # Initial queryset - all quotations
    quotations = Quotation.objects.all()

    # Apply status filter
    if status and status != 'All':
        quotations = quotations.filter(status=status)

    # Salesman restriction (same logic as SalesOrder)
    if request.user.is_authenticated and hasattr(request.user, 'role') and request.user.role.role == 'Salesman':
        salesman_name = request.user.first_name
        quotations = quotations.filter(salesman__salesman_name=salesman_name)
    elif salesman_filter and salesman_filter != 'All':
        quotations = quotations.filter(salesman__salesman_name=salesman_filter)

    # Apply date filters
    if start_date:
        quotations = quotations.filter(quotation_date__gte=start_date)
    if end_date:
        quotations = quotations.filter(quotation_date__lte=end_date)

    # Get all unique salesmen for the filter dropdown
    all_salesmen = Salesman.objects.all().order_by('salesman_name')

    # Pagination - 12 items per page (3x4 grid)
    paginator = Paginator(quotations.order_by('-quotation_number'), 12)

    try:
        quotations_page = paginator.page(page)
    except PageNotAnInteger:
        quotations_page = paginator.page(1)
    except EmptyPage:
        quotations_page = paginator.page(paginator.num_pages)

    return render(request, 'so/quotations/view_quotations.html', {
        'quotations': quotations_page,
        'all_salesmen': all_salesmen,
        'selected_salesman': salesman_filter,
        'current_status': status,
        'start_date': start_date,
        'end_date': end_date,
    })

from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.contrib import messages
from .models import Quotation, QuotationItem

def view_quotation_details(request, quotation_id):
    quotation = get_object_or_404(Quotation, id=quotation_id)
    quotation_items = quotation.items.all()

    # ✅ Compute totals & undercost logic
    grand_total = 0
    has_undercost_items = False
    for item in quotation_items:
        item.line_total = item.quantity * item.price
        grand_total += item.line_total

        # Check undercost
        if hasattr(item, "item") and item.item:
            undercost_limit = item.item.item_cost  # 10% above cost  
            item.is_undercost = item.price < undercost_limit
            if item.is_undercost:
                has_undercost_items = True
        else:
            item.is_undercost = False

    # 🔹 Automatic approval if no undercost items
    if not has_undercost_items and quotation.status != 'Approved':
        quotation.status = 'Approved'
        quotation.save()
        messages.success(request, "Quotation auto-approved as all prices are above minimum selling price.")

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'update_remarks':
            quotation.remarks = request.POST.get('remarks', '')
            quotation.save()
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': True,
                    'remarks': quotation.remarks
                })
            else:
                messages.success(request, 'Remarks updated successfully!')
                return redirect('view_quotation_details', quotation_id=quotation_id)

        elif action == 'approve':
            quotation.status = 'Approved'
            quotation.save()
            messages.success(request, 'Quotation approved successfully!')
            return redirect('view_quotation_details', quotation_id=quotation_id)

        elif action == 'hold':
            quotation.status = 'On Hold'
            quotation.save()
            messages.warning(request, 'Quotation put on hold.')
            return redirect('view_quotation_details', quotation_id=quotation_id)

    return render(request, "so/quotations/view_quotation_details.html", {
        "quotation": quotation,
        "quotation_items": quotation_items,
        "grand_total": grand_total,
        "has_undercost_items": has_undercost_items,
    })



from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction
from .models import Quotation, QuotationItem, Customer, Salesman, Items

@csrf_exempt
@transaction.atomic
def edit_quotation(request, quotation_id):
    quotation = get_object_or_404(Quotation, id=quotation_id)

    if request.method == 'POST':
        customer = get_object_or_404(Customer, id=request.POST.get('customer'))
        salesman = get_object_or_404(Salesman, id=request.POST.get('salesman')) if request.POST.get('salesman') else None
        

        # Update main quotation fields
        quotation.customer = customer
        quotation.salesman = salesman
        # if valid_until:
        #     quotation.valid_until = valid_until
        # quotation.save()

        # Get new items from POST
        item_ids = request.POST.getlist('item')  # dropdown selection
        quantities = request.POST.getlist('quantity')
        prices = request.POST.getlist('price')

        # Validate we have the same number of items, quantities, and prices
        if len(item_ids) != len(quantities) or len(item_ids) != len(prices):
            messages.error(request, 'Invalid form data: mismatched item fields')
            return redirect('edit_quotation', quotation_id=quotation.id)

        quotation_items = []
        has_undercost_items = False
        
        for i, (item_id, qty, price) in enumerate(zip(item_ids, quantities, prices)):
            if not item_id:  # Skip empty items
                continue
                
            try:
                quantity = int(qty) if qty else 0
                price = float(price) if price else 0.0
                
                if quantity <= 0 or price < 0:
                    continue  # Skip invalid items
                    
                # Get item from Items table
                item = get_object_or_404(Items, id=item_id)
                
                # Check if item is undercost
                undercost_limit = item.item_cost  # 10% above cost
                if price < undercost_limit:
                    has_undercost_items = True
                
                quotation_items.append(QuotationItem(
                    quotation=quotation,
                    item=item,  # Link to Items model
                    quantity=quantity,
                    price=price,
                    line_total=quantity * price
                ))
            except (ValueError, Items.DoesNotExist):
                # Skip invalid items but continue processing others
                continue

        # Only proceed if we have valid items
        if quotation_items:
            # Remove old items
            quotation.items.all().delete()
            
            # Bulk create new items
            QuotationItem.objects.bulk_create(quotation_items)

            # Recalculate totals
            total = sum(qi.line_total for qi in quotation_items)
            quotation.total_amount = total
            quotation.grand_total = total
            
            # 🔥 Update status based on undercost items
            if has_undercost_items:
                quotation.status = 'Pending'
                status_message = 'Quotation updated! Status changed to Pending due to undercost items.'
            else:
                quotation.status = 'Approved'
                status_message = 'Quotation updated and auto-approved! All prices are above minimum selling price.'
            
            quotation.save()
            
            messages.success(request, status_message)
        else:
            messages.error(request, 'No valid items found in the quotation')

        return redirect('view_quotation_details', quotation_id=quotation.id)

    # GET request → render form
    salesmen = Salesman.objects.all()
    firms = Items.objects.values_list('item_firm', flat=True).distinct()

    return render(request, 'so/quotations/edit_quotation.html', {
        'quotation': quotation,
        'salesmen': salesmen,
        'firms': firms,
        'quotation_items': quotation.items.all(),
    })

import os
import requests
from io import BytesIO
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.conf import settings

import os
import requests
from io import BytesIO
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.conf import settings

from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle,
    PageBreak, Frame, BaseDocTemplate
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageTemplate, KeepTogether

# --- PDF Styles ---
styles = getSampleStyleSheet()

# Add custom styles only if they don't exist to avoid KeyError
if 'MainTitle' not in styles:
    styles.add(ParagraphStyle(
        name='MainTitle', 
        fontSize=18, 
        leading=22, 
        alignment=TA_CENTER, 
        spaceAfter=12, 
        fontName='Helvetica-Bold'
    ))

if 'SectionHeader' not in styles:
    styles.add(ParagraphStyle(
        name='SectionHeader', 
        fontSize=12, 
        leading=14, 
        alignment=TA_LEFT, 
        spaceAfter=6, 
        fontName='Helvetica-Bold', 
        textColor=HexColor("#FFFFFF")
    ))

if 'ItemDescription' not in styles:
    styles.add(ParagraphStyle(
        name='ItemDescription', 
        fontSize=10, 
        leading=12, 
        alignment=TA_LEFT
    ))

if 'ItemDescriptionUndercost' not in styles:
    styles.add(ParagraphStyle(
        name='ItemDescriptionUndercost', 
        fontSize=10, 
        leading=12, 
        alignment=TA_LEFT, 
        textColor='red'
    ))

# Ensure h3 style exists (it should be in the base stylesheet)
if 'h3' not in styles:
    styles.add(ParagraphStyle(
        name='h3',
        parent=styles['Heading3'],
        fontSize=14,
        leading=16,
        spaceAfter=12,
        fontName='Helvetica-Bold'
    ))

# --- Helper Class for PDF Generation ---
class QuotationPDFTemplate(BaseDocTemplate):
    """
    A custom document template for creating professional quotations.
    This class handles the page layout, headers, footers, and page numbering.
    """
    def __init__(self, filename, **kwargs):
        # Set default values before calling super()
        self.company_name = "Junaid Sanitary & Electrical Trading LLC"
        self.company_address = "Dubai Investment Parks 2, Dubai, UAE"
        self.company_contact = "Email: sales@junaid.ae | Phone: +97142367723"
        self.page_count = 1  # Initialize with page 1
        self.logo_path = None
        
        # Increase bottom margin to provide more space for footer
        kwargs.setdefault('bottomMargin', 1.0 * inch)
        
        # Initialize the base class first
        super().__init__(filename, **kwargs)
        
        # Now set the logo after initialization
        self.logo_path = self._get_logo()

        # Define the content area (frame) with proper spacing for footer
        top_margin = 1.75 * inch
        bottom_margin = 1.0 * inch  # Increased for footer space
        
        frame = Frame(
            self.leftMargin,
            self.bottomMargin,
            self.width,
            self.height - top_margin - bottom_margin,
            leftPadding=0,
            rightPadding=0,
            bottomPadding=0,
            topPadding=0,
            id='normal'
        )
        
        template = PageTemplate(id='QuotationPage', frames=[frame], onPage=self.on_page)
        self.addPageTemplates([template])

    def _get_logo(self):
        """Fetches logo from URL with a local fallback."""
        try:
            logo_url = "https://junaidworld.com/wp-content/uploads/2023/09/footer-logo.png.webp"
            response_img = requests.get(logo_url, timeout=5)
            if response_img.status_code == 200:
                return Image(BytesIO(response_img.content), width=1.5*inch, height=0.5*inch)
        except Exception:
            try:
                # Fallback to a local file
                local_path = os.path.join(settings.BASE_DIR, 'static', 'images', 'logo.png')
                if os.path.exists(local_path):
                    return Image(local_path, width=1.5*inch, height=0.5*inch)
            except Exception:
                return None
        return None

    def on_page(self, canvas, doc):
        """This method is called on every page. It draws the header and footer."""
        self._header(canvas, doc)
        self._footer(canvas, doc)

    def _header(self, canvas, doc):
        """Draws the header on each page."""
        canvas.saveState()
        
        # Company Logo and Info Table
        header_content = []
        if self.logo_path:
            header_content.append([self.logo_path, f'{self.company_name}\n{self.company_address}\n{self.company_contact}'])
        else:
            header_content.append(['', f'{self.company_name}\n{self.company_address}\n{self.company_contact}'])

        header_table = Table(header_content, colWidths=[1.7*inch, 5.8*inch])
        header_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
            ('FONTNAME', (1, 0), (1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (1, 0), (1, 0), 9),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ]))
        
        w, h = header_table.wrap(doc.width, doc.topMargin)
        header_table.drawOn(canvas, doc.leftMargin, doc.height + doc.topMargin - h)

        # Bottom border for header
        canvas.setStrokeColor(HexColor('#2C5530'))
        canvas.setLineWidth(2)
        canvas.line(doc.leftMargin, doc.height + doc.topMargin - h - 5, 
                   doc.leftMargin + doc.width, doc.height + doc.topMargin - h - 5)
        
        canvas.restoreState()

    def _footer(self, canvas, doc):
        """Draws the footer on each page."""
        canvas.saveState()
        
        # Use the current page number and total page count
        footer_text = Paragraph(f"Thank you for your business! | {self.company_name}", styles['Normal'])
        page_num_text = Paragraph(f"Page {canvas.getPageNumber()} of {self.page_count}", styles['Normal'])
        
        # Table for alignment
        footer_table = Table([[footer_text, page_num_text]], colWidths=[doc.width/2, doc.width/2])
        footer_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (0, 0), 'LEFT'),
            ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
        ]))
        
        w, h = footer_table.wrap(doc.width, doc.bottomMargin)
        # Position footer higher to avoid content overlap
        footer_table.drawOn(canvas, doc.leftMargin, h + 0.2*inch)
        
        canvas.restoreState()
    
    def afterFlowable(self, flowable):
        """
        This is a hook that is called after each flowable is rendered.
        We use it to capture the total page count.
        """
        # Update the page count to the current page number
        # This will be the final page number when the document is complete
        self.page_count = self.page

# --- The Django View ---
def export_quotation_to_pdf(request, quotation_id):
    """
    Handles the request to generate and download a PDF for a specific quotation.
    """
    # Import your models here to avoid circular imports
    from .models import Quotation  # Adjust import path as needed
    
    quotation = get_object_or_404(Quotation, id=quotation_id)
    quotation_items = quotation.items.all()
    
    # Prepare HTTP response
    response = HttpResponse(content_type='application/pdf')
    filename = f"Quotation_{quotation.quotation_number}_{quotation.quotation_date.strftime('%Y%m%d')}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    
    buffer = BytesIO()
    
    # --- PDF Generation Logic ---
    doc = QuotationPDFTemplate(
        buffer,
        pagesize=A4,
        rightMargin=0.5*inch,
        leftMargin=0.5*inch,
        topMargin=0.5*inch,
        bottomMargin=1.0*inch  # Increased bottom margin for footer
    )
    
    elements = []
    
    # 1. Title and Status
    # Move the title and info section higher by reducing the initial spacer
    elements.append(Spacer(1, -1.3*inch))  # More negative value moves content further up

    title_table_data = [
        [Paragraph('QUOTATION', styles['MainTitle'])]
    ]
    title_table = Table(title_table_data, colWidths=[5.5*inch, 2*inch])
    title_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('FONTNAME', (1, 0), (1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (1, 0), (1, 0), 12),
        ('TEXTCOLOR', (1, 0), (1, 0), HexColor('#2C5530')),
    ]))
    elements.append(title_table)
    elements.append(Spacer(1, 0.1*inch))  # Reduce space after title

    # 2. Quotation Info and Customer Info (Side-by-side)
    # Set a common width for all main tables
    main_table_width = 7.5 * inch

    # Use Paragraph for all text to ensure consistent wrapping and height
    quotation_data = [
        [Paragraph('Quotation Details', styles['SectionHeader'])],
        [Paragraph(f'<b>Number:</b> {quotation.quotation_number}', styles['Normal'])],
        [Paragraph(f'<b>Date:</b> {quotation.quotation_date.strftime("%d-%b-%Y")}', styles['Normal'])],
    ]

    quotation_info_table = Table(quotation_data, colWidths=[main_table_width / 2])
    quotation_info_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 1), (-1, -1), 2),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#808080')),
        ('BACKGROUND', (0, 0), (0, 0), HexColor('#4A7C59')),
    ]))

    customer_data = [
        [Paragraph('Customer Information', styles['SectionHeader'])],
        [Paragraph(f'<b>Name:</b> {quotation.customer.customer_name}', styles['Normal'])],
    ]

    # Add empty rows to match the height of quotation info table
    # This ensures both tables have the same number of rows and appear balanced
    if quotation.salesman:
        customer_data.append([Paragraph(f'<b>Salesman:</b> {quotation.salesman.salesman_name}', styles['Normal'])])
    else:
        # Add an empty row to match height if no salesman
        customer_data.append([Paragraph('', styles['Normal'])])

    customer_info_table = Table(customer_data, colWidths=[main_table_width / 2])
    customer_info_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 1), (-1, -1), 2),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#808080')),
        ('BACKGROUND', (0, 0), (0, 0), HexColor('#4A7C59')),
    ]))

    info_table = Table([[quotation_info_table, customer_info_table]], colWidths=[main_table_width / 2, main_table_width / 2])
    info_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 0.2 * inch))

    # 3. Items Table with pagination handling
    items_header = ['#', 'UPC Code', 'Description', 'Qty', 'Unit Price', 'Total']
    items_data = [items_header]
    subtotal = 0.0

    for idx, item in enumerate(quotation_items, 1):
        line_total = item.quantity * item.price
        subtotal += line_total

        desc_style = styles['ItemDescription']
        description_text = getattr(item.item, 'item_description', 'No description available')
        description_para = Paragraph(description_text, desc_style)

        items_data.append([
            str(idx),
            getattr(item.item, 'item_upvc', 'N/A'),
            description_para,
            f"{item.quantity} {item.unit}",
            f"AED {item.price:,.2f}",
            f"AED {line_total:,.2f}"
        ])

    # Calculate table dimensions for better pagination control
    items_table = Table(
        items_data,
        colWidths=[
            main_table_width * 0.04,   # #
            main_table_width * 0.12,   # Item Code
            main_table_width * 0.47,   # Description
            main_table_width * 0.07,   # Qty
            main_table_width * 0.15,   # Unit Price
            main_table_width * 0.15    # Total
        ],
        repeatRows=1
    )
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), HexColor('#2C5530')),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#808080')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [HexColor('#F0F7F4'), white]),
        ('ALIGN', (0, 1), (1, -1), 'CENTER'),
        ('ALIGN', (3, 1), (3, -1), 'CENTER'),
        ('ALIGN', (4, 1), (-1, -1), 'RIGHT'),
    ]))
    
    # Wrap items table for alignment and add page break protection
    items_wrapper = Table([[items_table]], colWidths=[main_table_width])
    items_wrapper.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    
    # Add the items table - ReportLab will handle pagination automatically
    # The table has repeatRows=1 so headers will repeat on each page
    elements.append(items_wrapper)
    elements.append(Spacer(1, 0.1 * inch))

    # 4. Summary Table (Subtotal, VAT, Grand Total)
    tax_rate = 0.05
    tax_amount = subtotal * tax_rate
    grand_total = subtotal + tax_amount

    summary_data = [
        ['Subtotal:', f"AED {subtotal:,.2f}"],
        [f'VAT ({tax_rate:.0%}):', f"AED {tax_amount:,.2f}"],
        ['Grand Total:', f"AED {grand_total:,.2f}"],
    ]
    summary_table = Table(summary_data, colWidths=[main_table_width * 0.5, main_table_width * 0.5])
    summary_table.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#808080')),
        ('FONTNAME', (0, 2), (-1, 2), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 2), (-1, 2), 12),
        ('BACKGROUND', (0, 2), (-1, 2), HexColor('#2C5530')),
        ('TEXTCOLOR', (0, 2), (-1, 2), white),
    ]))
    
    # Wrap summary table for alignment and ensure it stays with the items table
    summary_wrapper = Table([[summary_table]], colWidths=[main_table_width])
    summary_wrapper.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    
    # Use KeepTogether to prevent summary from being separated from the last items
    elements.append(KeepTogether(summary_wrapper))
    elements.append(Spacer(1, 0.3 * inch))
    
    # 5. Remarks and Terms & Conditions
    if hasattr(quotation, 'remarks') and quotation.remarks:
        remarks_section = [
            Paragraph("Remarks:", styles['h3']),
            Paragraph(quotation.remarks, styles['Normal']),
            Spacer(1, 0.2 * inch)
        ]
        elements.extend(remarks_section)

    terms_section = [
        Paragraph("Terms & Conditions:", styles['h3'])
    ]
    terms = [
        "1. This quotation is valid for 30 days from the date of issue.",
        "2. Prices are subject to change without prior notice after the validity period.",
        "3. Delivery timelines will be confirmed upon order confirmation.",
        "4. This is a system-generated document and does not require a signature.",
    ]
    for term in terms:
        terms_section.append(Paragraph(term, styles['Normal']))
    
    # Add terms and conditions as a group
    elements.extend(terms_section)
    
    # Build the PDF
    doc.multiBuild(elements)
    
    pdf = buffer.getvalue()
    buffer.close()
    response.write(pdf)
    
    return response