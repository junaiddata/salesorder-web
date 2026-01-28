from django.views.decorators.csrf import csrf_exempt
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from .models import Quotation, QuotationItem, Customer, Salesman, Items, CustomerPrice
from .models import Quotation, QuotationItem, CustomerPrice, Customer, Items, Salesman, QuotationLog
from .utils import get_client_ip, label_network
from django.db.models import Q
from .utils import parse_device_info


@csrf_exempt
@transaction.atomic
def create_quotation(request):
    if request.method == 'POST':
        try:
            # -------------------------
            # 0. Division & Remarks Setup
            # -------------------------
            division = 'JUNAID' # Default
            if request.user.is_authenticated and 'alabama' in request.user.username.lower():
                division = 'ALABAMA'
            
            remarks = request.POST.get('remarks', '').strip()

            # -------------------------
            # 1. Customer handling
            # -------------------------
            customer_id = request.POST.get('customer')
            customer_display_name = request.POST.get('new_customer_name', '').strip()  # Used as display name for CASH customers

            if not customer_id:
                messages.error(request, 'Please select a customer.')
                return redirect('create_quotation')

            # Get salesman FIRST (required)
            salesman_id = request.POST.get('salesman')
            if not salesman_id:
                messages.error(request, 'Salesman is required.')
                return redirect('create_quotation')
                
            salesman = get_object_or_404(Salesman, id=salesman_id)
            customer = get_object_or_404(Customer, id=customer_id)

            # -------------------------
            # 2. Items validation
            # -------------------------
            item_ids = request.POST.getlist('item')
            quantities = request.POST.getlist('quantity')
            prices = request.POST.getlist('price')
            units = request.POST.getlist('unit')

            # Check if we have items
            if not item_ids:
                messages.error(request, 'Please add at least one item.')
                return redirect('create_quotation')

            if len(item_ids) != len(quantities) or len(item_ids) != len(prices) or len(item_ids) != len(units):
                messages.error(request, 'Invalid form data. Please try again.')
                return redirect('create_quotation')

            # -------------------------
            # 3. Create Quotation Object
            # -------------------------
            quotation = Quotation.objects.create(
                customer=customer,
                salesman=salesman,
                division=division,
                remarks=remarks,
                customer_display_name=customer_display_name if customer_display_name else None
            )

            # -------------------------
            # 4. Logging Logic
            # -------------------------
            ip = get_client_ip(request)
            network_label = label_network(ip)
            ua_string = request.META.get('HTTP_USER_AGENT', '')[:500]
            device_type, device_os, device_browser = parse_device_info(ua_string)

            try:
                lat = request.POST.get("location_lat")
                lng = request.POST.get("location_lng")
                lat_val = float(lat) if lat not in (None, "",) else None
                lng_val = float(lng) if lng not in (None, "",) else None
            except ValueError:
                lat_val = None
                lng_val = None

            QuotationLog.objects.create(
                quotation=quotation,
                user=request.user if request.user.is_authenticated else None,
                ip_address=ip,
                user_agent=ua_string,
                device_type=device_type,
                device_os=device_os,
                device_browser=device_browser,
                location_lat=lat_val,
                location_lng=lng_val,
                network_label=network_label,
                device=getattr(request, 'device_obj', None), 
                action="created",
            )

            # -------------------------
            # 5. Process Items
            # -------------------------
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
            quotation.grand_total = total_amount # Add Tax logic here if needed
            quotation.save()

            messages.success(request, f'Quotation {quotation.quotation_number} created successfully!')
            return redirect('view_quotations')

        except Exception as e:
            messages.error(request, f'An error occurred: {str(e)}')
            return redirect('create_quotation')

    else:
       # GET request → render empty form
        customers = Customer.objects.all().order_by('customer_name')
        
        # ---------------------------------------------------------------------
        # START: Salesman Filtering Logic
        # ---------------------------------------------------------------------
        salesmen = Salesman.objects.all()

        # Filter Logic: Only apply if user is logged in AND is NOT a superuser/admin
        if request.user.is_authenticated and not request.user.is_superuser:
            current_username = request.user.username.lower()

            user_salesman_map = {
                'alabamakadhar': ['KADER'],
                'alabamamusharaf': ['MUSHARAF'],   # Multiple allowed
                'alabamaadmin': ['KADER','MUSHARAF','AIJAZ','CASH'],               # Single allowed
                
            }


            if current_username in user_salesman_map:
                target_names = user_salesman_map[current_username]
                
                # Build a complex query: (name contains A) OR (name contains B)
                query = Q()
                for name in target_names:
                    query |= Q(salesman_name__icontains=name)
                
                salesmen = salesmen.filter(query)
        # ---------------------------------------------------------------------
        # END: Salesman Filtering Logic
        # ---------------------------------------------------------------------
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
from django.db.models import Q
from django.template.loader import render_to_string
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.contrib.auth.decorators import login_required

def view_quotations(request):
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    salesman_filter = request.GET.get('salesman_filter')
    page = request.GET.get('page', 1)
    status = request.GET.get('status', 'All')  # Default to 'All'
    division = request.GET.get('division', 'All')
    q = (request.GET.get('q') or '').strip()

    # Initial queryset - all quotations
    quotations = Quotation.objects.all()

    # Apply status filter
    if status and status != 'All':
        quotations = quotations.filter(status=status)
    
    # Apply user-based division restrictions
    if hasattr(request.user, 'role') and request.user.role.role == 'Admin':
        if request.user.username.lower() == 'alabamaadmin':
            quotations = quotations.filter(division='ALABAMA')
        elif request.user.username.lower() not in ['so', 'manager']:
            quotations = quotations.filter(division='JUNAID')
        # else: so/manager see all quotations - no additional filter


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

    # Division filter
    if division and division != 'All':
        div = (division or '').strip().upper()
        if div in ('ALABAMA', 'JUNAID'):
            quotations = quotations.filter(division=div)

    # Search query (backend)
    if q:
        quotations = quotations.filter(
            Q(quotation_number__icontains=q)
            | Q(customer__customer_name__icontains=q)
            | Q(customer__customer_code__icontains=q)
            | Q(salesman__salesman_name__icontains=q)
            | Q(remarks__icontains=q)
            | Q(customer_display_name__icontains=q)
        )

    # Get all unique salesmen for the filter dropdown
    all_salesmen = Salesman.objects.all().order_by('salesman_name')

    # Pagination - 12 items per page (3x4 grid)
    paginator = Paginator(quotations.order_by('-created_at'), 12)

    try:
        quotations_page = paginator.page(page)
    except PageNotAnInteger:
        quotations_page = paginator.page(1)
    except EmptyPage:
        quotations_page = paginator.page(paginator.num_pages)

    # Build query string for pagination links
    query_params = []
    if status and status != 'All':
        query_params.append(f"status={status}")
    if start_date:
        query_params.append(f"start_date={start_date}")
    if end_date:
        query_params.append(f"end_date={end_date}")
    if salesman_filter:
        query_params.append(f"salesman_filter={salesman_filter}")
    if division and division != 'All':
        query_params.append(f"division={division}")
    if q:
        query_params.append(f"q={q}")
    query_string = "&".join(query_params)

    return render(request, 'so/quotations/view_quotations.html', {
        'quotations': quotations_page,
        'all_salesmen': all_salesmen,
        'selected_salesman': salesman_filter,
        'current_status': status,
        'start_date': start_date,
        'end_date': end_date,
        'selected_division': division or 'All',
        'search_query': q,
        'query_string': query_string,
    })


@login_required
@require_GET
def view_quotations_ajax(request):
    """
    AJAX endpoint for backend filtering/search/pagination on quotations dashboard.
    Supports:
    - q (search)
    - status
    - start_date / end_date
    - salesman_filter
    - division: All | ALABAMA | JUNAID
    - page
    """
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    salesman_filter = request.GET.get('salesman_filter')
    page = request.GET.get('page', 1)
    status = request.GET.get('status', 'All')
    division = request.GET.get('division', 'All')
    q = (request.GET.get('q') or '').strip()

    quotations = Quotation.objects.all()

    if status and status != 'All':
        quotations = quotations.filter(status=status)

    # Apply user-based division restrictions
    if hasattr(request.user, 'role') and request.user.role.role == 'Admin':
        if request.user.username.lower() == 'alabamaadmin':
            quotations = quotations.filter(division='ALABAMA')
        elif request.user.username.lower() not in ['so', 'manager']:
            quotations = quotations.filter(division='JUNAID')
        # else: so/manager see all quotations - no additional filter

    if request.user.is_authenticated and hasattr(request.user, 'role') and request.user.role.role == 'Salesman':
        salesman_name = request.user.first_name
        quotations = quotations.filter(salesman__salesman_name=salesman_name)
    elif salesman_filter and salesman_filter != 'All':
        quotations = quotations.filter(salesman__salesman_name=salesman_filter)

    if start_date:
        quotations = quotations.filter(quotation_date__gte=start_date)
    if end_date:
        quotations = quotations.filter(quotation_date__lte=end_date)

    if division and division != 'All':
        div = (division or '').strip().upper()
        if div in ('ALABAMA', 'JUNAID'):
            quotations = quotations.filter(division=div)

    if q:
        quotations = quotations.filter(
            Q(quotation_number__icontains=q)
            | Q(customer__customer_name__icontains=q)
            | Q(customer__customer_code__icontains=q)
            | Q(salesman__salesman_name__icontains=q)
            | Q(remarks__icontains=q)
            | Q(customer_display_name__icontains=q)
        )

    paginator = Paginator(quotations.order_by('-created_at'), 12)
    try:
        quotations_page = paginator.page(page)
    except PageNotAnInteger:
        quotations_page = paginator.page(1)
    except EmptyPage:
        quotations_page = paginator.page(paginator.num_pages)

    query_params = []
    if status and status != 'All':
        query_params.append(f"status={status}")
    if start_date:
        query_params.append(f"start_date={start_date}")
    if end_date:
        query_params.append(f"end_date={end_date}")
    if salesman_filter:
        query_params.append(f"salesman_filter={salesman_filter}")
    if division and division != 'All':
        query_params.append(f"division={division}")
    if q:
        query_params.append(f"q={q}")
    query_string = "&".join(query_params)

    html = render_to_string(
        'so/quotations/_quotations_results.html',
        {
            'quotations': quotations_page,
            'current_status': status or 'All',
            'selected_salesman': salesman_filter,
            'selected_division': division or 'All',
            'start_date': start_date,
            'end_date': end_date,
            'query_string': query_string,
        },
        request=request
    )

    return JsonResponse({'html': html, 'count': paginator.count})

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
        customer_display_name = request.POST.get('customer_display_name', '').strip()

        # Update main quotation fields
        quotation.customer = customer
        quotation.salesman = salesman
        quotation.customer_display_name = customer_display_name if customer_display_name else None

        # Get new items from POST
        item_ids = request.POST.getlist('item')  # dropdown selection
        quantities = request.POST.getlist('quantity')
        prices = request.POST.getlist('price')
        units = request.POST.getlist('unit')

        # Validate we have the same number of items, quantities, prices, and units
        if len(item_ids) != len(quantities) or len(item_ids) != len(prices) or len(item_ids) != len(units):
            messages.error(request, 'Invalid form data: mismatched item fields')
            return redirect('edit_quotation', quotation_id=quotation.id)

        quotation_items = []
        has_undercost_items = False
        
        for i, (item_id, qty, price, unit) in enumerate(zip(item_ids, quantities, prices, units)):
            if not item_id:  # Skip empty items
                continue
                
            try:
                quantity = int(qty) if qty else 0
                price_val = float(price) if price else 0.0
                unit_val = unit if unit in ['pcs', 'ctn', 'roll'] else 'pcs'
                
                if quantity <= 0 or price_val < 0:
                    continue  # Skip invalid items
                    
                # Get item from Items table
                item = get_object_or_404(Items, id=item_id)
                
                # Check if item is undercost
                undercost_limit = item.item_cost  # 10% above cost
                if price_val < undercost_limit:
                    has_undercost_items = True
                
                quotation_items.append(QuotationItem(
                    quotation=quotation,
                    item=item,  # Link to Items model
                    quantity=quantity,
                    price=price_val,
                    unit=unit_val,
                    line_total=quantity * price_val
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

    # Filter Logic: Only apply if user is logged in AND is NOT a superuser/admin
    if request.user.is_authenticated and not request.user.is_superuser:
        current_username = request.user.username.lower()

        # Define Mapping: 'username' -> List of ['Name1', 'Name2']
        user_salesman_map = {
            'alabamakadhar': ['KADER'],
            'alabamamusharaf': ['MUSHARAF'],   # Multiple allowed
            'alabamaadmin': ['KADER','MUSHARAF','AIJAZ','CASH'],               # Single allowed
            
        }


        if current_username in user_salesman_map:
            target_names = user_salesman_map[current_username]
            
            # Build a complex query: (name contains A) OR (name contains B)
            query = Q()
            for name in target_names:
                query |= Q(salesman_name__icontains=name)
            
            salesmen = salesmen.filter(query)
    # -------------------------------------------------------------------------
    # END: Salesman Filtering Logic
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

from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle,
    PageBreak, Frame, BaseDocTemplate, PageTemplate, KeepTogether
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
# Adjust import based on your actual app name
from .models import Quotation 

# --- PDF Styles (Shared) ---
styles = getSampleStyleSheet()

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

if 'ItemCode' not in styles:
    styles.add(ParagraphStyle(
        name='ItemCode',
        parent=styles['Normal'],
        fontSize=9,
        leading=10,
        alignment=TA_CENTER,
        wordWrap='CJK'
    ))

if 'h3' not in styles:
    styles.add(ParagraphStyle(
        name='h3',
        parent=styles['Heading3'],
        fontSize=14,
        leading=16,
        spaceAfter=12,
        fontName='Helvetica-Bold'
    ))

# --- Dynamic Template Class ---
class QuotationPDFTemplate(BaseDocTemplate):
    """
    Dynamic template that accepts company details and theme colors.
    """
    def __init__(self, filename, company_config, theme_config, **kwargs):
        self.company_name = company_config.get('name', "JUNAID SANITARY & ELECTRICAL REQUISITES TRADING LLC")
        self.company_address = company_config.get('address', "Dubai Investment Parks 2, Dubai, UAE")
        self.company_contact = company_config.get('contact', "Email: sales@junaid.ae | Phone: +97142367723")
        self.logo_url = company_config.get('logo_url')
        self.local_logo_path = company_config.get('local_logo_path')
        
        self.theme_color = theme_config.get('primary', HexColor('#2C5530'))
        
        self.page_count = 1 
        self.logo_image = None
        
        kwargs.setdefault('bottomMargin', 1.0 * inch)
        super().__init__(filename, **kwargs)
        
        self.logo_image = self._get_logo()

        top_margin = 1.75 * inch
        bottom_margin = 1.0 * inch
        
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
        # Try URL first
        if self.logo_url:
            try:
                response_img = requests.get(self.logo_url, timeout=5)
                if response_img.status_code == 200:
                    return Image(BytesIO(response_img.content), width=1.5*inch, height=0.5*inch)
            except Exception:
                pass
        
        # Try Local fallback
        if self.local_logo_path:
            try:
                if os.path.exists(self.local_logo_path):
                    return Image(self.local_logo_path, width=1.5*inch, height=0.5*inch)
            except Exception:
                pass
        return None

    def on_page(self, canvas, doc):
        self._header(canvas, doc)
        self._footer(canvas, doc)

    def _header(self, canvas, doc):
        canvas.saveState()
        
        header_content = []
        company_text = f'{self.company_name}\n{self.company_address}\n{self.company_contact}'
        
        if self.logo_image:
            header_content.append([self.logo_image, company_text])
        else:
            header_content.append(['', company_text])

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

        # Line color based on theme
        canvas.setStrokeColor(self.theme_color)
        canvas.setLineWidth(2)
        canvas.line(doc.leftMargin, doc.height + doc.topMargin - h - 5, 
                   doc.leftMargin + doc.width, doc.height + doc.topMargin - h - 5)
        
        canvas.restoreState()

    def _footer(self, canvas, doc):
        canvas.saveState()
        
        footer_text = Paragraph(f"Thank you for your business! | {self.company_name}", styles['Normal'])
        page_num_text = Paragraph(f"Page {canvas.getPageNumber()} of {self.page_count}", styles['Normal'])
        
        footer_table = Table([[footer_text, page_num_text]], colWidths=[doc.width/2, doc.width/2])
        footer_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (0, 0), 'LEFT'),
            ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
        ]))
        
        w, h = footer_table.wrap(doc.width, doc.bottomMargin)
        footer_table.drawOn(canvas, doc.leftMargin, h + 0.2*inch)
        
        canvas.restoreState()
    
    def afterFlowable(self, flowable):
        self.page_count = self.page

# ==========================================
# 1. MAIN DISPATCHER VIEW
# ==========================================
def export_quotation_to_pdf(request, quotation_id):
    quotation = get_object_or_404(Quotation, id=quotation_id)
    
    # Setup Response
    response = HttpResponse(content_type='application/pdf')
    filename = f"Quotation_{quotation.quotation_number}_{quotation.quotation_date.strftime('%Y%m%d')}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    
    buffer = BytesIO()
    
    # Dispatch based on Division
    if quotation.division == 'ALABAMA':
        generate_alabama_quotation(buffer, quotation)
    else:
        generate_junaid_quotation(buffer, quotation)
        
    pdf_content = buffer.getvalue()
    buffer.close()
    response.write(pdf_content)
    return response

# ==========================================
# 2. JUNAID GENERATOR (Green/White Theme)
# ==========================================
def generate_junaid_quotation(buffer, quotation):
    quotation_items = quotation.items.all()
    
    # --- CONFIGURATION ---
    company_config = {
        'name': "JUNAID SANITARY & ELECTRICAL REQUISITES TRADING LLC",
        'address': "Dubai Investment Parks 2, Dubai, UAE",
        'contact': "Email: sales@junaid.ae | Phone: +97142367723",
        'logo_url': "https://junaidworld.com/wp-content/uploads/2023/09/footer-logo.png.webp",
        'local_logo_path': os.path.join(settings.BASE_DIR, 'static', 'images', 'footer-logo.png.webp')
    }
    
    # Colors
    PRIMARY_COLOR = HexColor('#2C5530')  # Dark Green
    SECONDARY_COLOR = HexColor('#4A7C59') # Light Green
    ROW_BG_COLOR = HexColor('#F0F7F4')    # Very light green
    
    theme_config = {'primary': PRIMARY_COLOR}

    # --- DOCUMENT SETUP ---
    main_table_width = 7.2 * inch 
    doc = QuotationPDFTemplate(
        buffer,
        company_config=company_config,
        theme_config=theme_config,
        pagesize=A4,
        rightMargin=0.5*inch,
        leftMargin=0.5*inch,
        topMargin=0.5*inch,
        bottomMargin=1.0*inch
    )
    
    elements = []
    
    # --- Title ---
    elements.append(Spacer(1, -1.3*inch))
    title_table_data = [[Paragraph('QUOTATION', styles['MainTitle'])]]
    title_table = Table(title_table_data, colWidths=[5.2*inch, 2*inch])
    title_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('FONTNAME', (1, 0), (1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (1, 0), (1, 0), 12),
        ('TEXTCOLOR', (1, 0), (1, 0), PRIMARY_COLOR),
    ]))
    elements.append(title_table)
    elements.append(Spacer(1, 0.1*inch))

    # --- Info Tables ---
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
        ('BACKGROUND', (0, 0), (0, 0), SECONDARY_COLOR),
    ]))

    # Use customer_display_name if available, otherwise use customer.customer_name
    display_name = quotation.customer_display_name or quotation.customer.customer_name
    customer_data = [
        [Paragraph('Customer Information', styles['SectionHeader'])],
        [Paragraph(f'<b>Name:</b> {display_name}', styles['Normal'])],
    ]

    if quotation.salesman:
        customer_data.append([Paragraph(f'<b>Salesman:</b> {quotation.salesman.salesman_name}', styles['Normal'])])
    else:
        customer_data.append([Paragraph('', styles['Normal'])])

    customer_info_table = Table(customer_data, colWidths=[main_table_width / 2])
    customer_info_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 1), (-1, -1), 2),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#808080')),
        ('BACKGROUND', (0, 0), (0, 0), SECONDARY_COLOR),
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

    # --- Items Table ---
    items_header = ['#', 'UPC Code', 'Description', 'Qty', 'Unit Price', 'Total']
    items_data = [items_header]
    subtotal = 0.0

    for idx, item in enumerate(quotation_items, 1):
        line_total = item.quantity * item.price
        subtotal += line_total

        desc_style = styles['ItemDescription']
        description_text = getattr(item.item, 'item_description', 'No description available')
        description_para = Paragraph(description_text, desc_style)

        upc_raw = getattr(item.item, 'item_upvc', '')
        if upc_raw is None: upc_raw = ""
        upc_para = Paragraph(str(upc_raw), styles['ItemCode'])

        items_data.append([
            str(idx),
            upc_para,
            description_para,
            f"{item.quantity} {item.unit}",
            f"AED {item.price:,.2f}",
            f"AED {line_total:,.2f}"
        ])

    items_table = Table(
        items_data,
        colWidths=[
            main_table_width * 0.05,
            main_table_width * 0.15,
            main_table_width * 0.43,
            main_table_width * 0.07,
            main_table_width * 0.15,
            main_table_width * 0.15
        ],
        repeatRows=1
    )
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), PRIMARY_COLOR),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#808080')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [ROW_BG_COLOR, white]),
        ('ALIGN', (0, 1), (1, -1), 'CENTER'),
        ('ALIGN', (3, 1), (3, -1), 'CENTER'),
        ('ALIGN', (4, 1), (-1, -1), 'RIGHT'),
    ]))
    
    elements.append(items_table)
    elements.append(Spacer(1, 0.1 * inch))

    # --- Summary Table ---
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
        ('BACKGROUND', (0, 2), (-1, 2), PRIMARY_COLOR),
        ('TEXTCOLOR', (0, 2), (-1, 2), white),
    ]))
    
    summary_wrapper = Table([[summary_table]], colWidths=[main_table_width])
    summary_wrapper.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    
    elements.append(KeepTogether(summary_wrapper))
    elements.append(Spacer(1, 0.3 * inch))
    
    # --- Remarks & Terms ---
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
    
    elements.extend(terms_section)
    
    doc.multiBuild(elements)

# ==========================================
# 3. ALABAMA GENERATOR (Red/Black Theme)
# ==========================================
def generate_alabama_quotation(buffer, quotation):
    quotation_items = quotation.items.all()
    
    # --- CONFIGURATION ---
    company_config = {
        'name': "ALABAMA BUILDING MATERIALS TRADING", # Or full legal name
        'address': "Dubai Investment Parks 2, Dubai, UAE",
        'contact': "Email: sales@alabamauae.com", # Update if needed
        'logo_url': "https://alabamauae.com/alabama4.png",
        'local_logo_path': os.path.join(settings.BASE_DIR, 'static', 'images', 'alabama-logo.png')
    }
    
    # Colors (Red & Black Theme)
    PRIMARY_COLOR = HexColor("#211F1F")  # Dark Red
    SECONDARY_COLOR = HexColor('#211F1F') # Black/Dark Grey
    ROW_BG_COLOR = HexColor('#FFF5F5')    # Very light red for rows
    
    theme_config = {'primary': PRIMARY_COLOR}

    # --- DOCUMENT SETUP ---
    main_table_width = 7.2 * inch 
    doc = QuotationPDFTemplate(
        buffer,
        company_config=company_config,
        theme_config=theme_config,
        pagesize=A4,
        rightMargin=0.5*inch,
        leftMargin=0.5*inch,
        topMargin=0.5*inch,
        bottomMargin=1.0*inch
    )
    
    elements = []
    
    # --- Title ---
    elements.append(Spacer(1, -1.3*inch))
    title_table_data = [[Paragraph('QUOTATION', styles['MainTitle'])]]
    title_table = Table(title_table_data, colWidths=[5.2*inch, 2*inch])
    title_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('FONTNAME', (1, 0), (1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (1, 0), (1, 0), 12),
        ('TEXTCOLOR', (1, 0), (1, 0), PRIMARY_COLOR),
    ]))
    elements.append(title_table)
    elements.append(Spacer(1, 0.1*inch))

    # --- Info Tables ---
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
        ('BACKGROUND', (0, 0), (0, 0), PRIMARY_COLOR), # Use Red for headers
    ]))

    # Use customer_display_name if available, otherwise use customer.customer_name
    display_name = quotation.customer_display_name or quotation.customer.customer_name
    customer_data = [
        [Paragraph('Customer Information', styles['SectionHeader'])],
        [Paragraph(f'<b>Name:</b> {display_name}', styles['Normal'])],
    ]

    if quotation.salesman:
        customer_data.append([Paragraph(f'<b>Salesman:</b> {quotation.salesman.salesman_name}', styles['Normal'])])
    else:
        customer_data.append([Paragraph('', styles['Normal'])])

    customer_info_table = Table(customer_data, colWidths=[main_table_width / 2])
    customer_info_table.setStyle(TableStyle([
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 1), (-1, -1), 2),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#808080')),
        ('BACKGROUND', (0, 0), (0, 0), PRIMARY_COLOR), # Use Red for headers
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

    # --- Items Table ---
    items_header = ['#', 'UPC Code', 'Description', 'Qty', 'Unit Price', 'Total']
    items_data = [items_header]
    subtotal = 0.0

    for idx, item in enumerate(quotation_items, 1):
        line_total = item.quantity * item.price
        subtotal += line_total

        desc_style = styles['ItemDescription']
        # Set text color to black/grey for readability
        desc_style.textColor = black 
        
        description_text = getattr(item.item, 'item_description', 'No description available')
        description_para = Paragraph(description_text, desc_style)

        upc_raw = getattr(item.item, 'item_upvc', '')
        if upc_raw is None: upc_raw = ""
        upc_para = Paragraph(str(upc_raw), styles['ItemCode'])

        items_data.append([
            str(idx),
            upc_para,
            description_para,
            f"{item.quantity} {item.unit}",
            f"AED {item.price:,.2f}",
            f"AED {line_total:,.2f}"
        ])

    items_table = Table(
        items_data,
        colWidths=[
            main_table_width * 0.05,
            main_table_width * 0.15,
            main_table_width * 0.43,
            main_table_width * 0.07,
            main_table_width * 0.15,
            main_table_width * 0.15
        ],
        repeatRows=1
    )
    items_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), SECONDARY_COLOR), # Black Header
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor('#808080')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [ROW_BG_COLOR, white]),
        ('ALIGN', (0, 1), (1, -1), 'CENTER'),
        ('ALIGN', (3, 1), (3, -1), 'CENTER'),
        ('ALIGN', (4, 1), (-1, -1), 'RIGHT'),
        ('FONTSIZE', (0, 1), (-1, -1), 8),  # Set row content fontsize to 9
    ]))
    
    elements.append(items_table)
    elements.append(Spacer(1, 0.1 * inch))

    # --- Summary Table ---
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
        ('BACKGROUND', (0, 2), (-1, 2), PRIMARY_COLOR), # Red Total Background
        ('TEXTCOLOR', (0, 2), (-1, 2), white),
    ]))
    
    summary_wrapper = Table([[summary_table]], colWidths=[main_table_width])
    summary_wrapper.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    
    elements.append(KeepTogether(summary_wrapper))
    elements.append(Spacer(1, 0.3 * inch))
    
    # --- Remarks & Terms ---
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
    
    elements.extend(terms_section)
    
    doc.multiBuild(elements)