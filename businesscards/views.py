from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse, Http404
from django.db.models import Q
from django.core.paginator import Paginator
from .models import SalesmanCard
from .forms import ExcelUploadForm, SalesmanCardForm, SalesmanCardForm
from .services import (
    validate_excel_file,
    import_excel_data,
    generate_qr_code,
    generate_vcard
)
import os
from django.conf import settings


@login_required
def dashboard(request):
    """Marketing dashboard with stats and quick actions"""
    total_cards = SalesmanCard.objects.count()
    recent_cards = SalesmanCard.objects.all()[:5]
    
    # Get unique departments
    departments = SalesmanCard.objects.values_list('department', flat=True).distinct()
    
    context = {
        'total_cards': total_cards,
        'recent_cards': recent_cards,
        'departments': departments,
    }
    
    return render(request, 'businesscards/dashboard.html', context)


@login_required
def upload_excel(request):
    """Excel upload form with preview"""
    preview_data = None
    validation_result = None
    
    if request.method == 'POST':
        form = ExcelUploadForm(request.POST, request.FILES)
        
        if form.is_valid():
            excel_file = form.cleaned_data['excel_file']
            company_name = form.cleaned_data.get('company_name', 'Company Name')
            
            # Check if this is a preview request
            if 'preview' in request.POST:
                # Validate and show preview
                validation_result = validate_excel_file(excel_file)
                if validation_result['valid']:
                    preview_data = validation_result['preview']
                    messages.info(request, f"Preview: {validation_result['total_rows']} rows found")
                else:
                    for error in validation_result['errors']:
                        messages.error(request, error)
            
            # Check if this is an import request
            elif 'import' in request.POST:
                # Import the data
                result = import_excel_data(excel_file, request.user, company_name, request=request)
                
                if result['success']:
                    messages.success(
                        request,
                        f"Import completed! Created: {result['created']}, Updated: {result['updated']}"
                    )
                    if result['errors']:
                        for error in result['errors']:
                            messages.warning(request, error)
                    return redirect('businesscards:salesmen_list')
                else:
                    for error in result['errors']:
                        messages.error(request, error)
    else:
        form = ExcelUploadForm()
    
    context = {
        'form': form,
        'preview_data': preview_data,
        'validation_result': validation_result,
    }
    
    return render(request, 'businesscards/upload_excel.html', context)


@login_required
def salesmen_list(request):
    """List all salesmen with search and filter"""
    salesmen = SalesmanCard.objects.all()
    
    # Search
    search_query = request.GET.get('search', '')
    if search_query:
        salesmen = salesmen.filter(
            Q(name__icontains=search_query) |
            Q(email__icontains=search_query) |
            Q(phone__icontains=search_query) |
            Q(designation__icontains=search_query)
        )
    
    # Filter by department
    department_filter = request.GET.get('department', '')
    if department_filter:
        salesmen = salesmen.filter(department=department_filter)
    
    # Pagination
    paginator = Paginator(salesmen, 25)  # 25 per page
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    # Get unique departments for filter dropdown
    departments = SalesmanCard.objects.values_list('department', flat=True).distinct()
    
    context = {
        'page_obj': page_obj,
        'search_query': search_query,
        'department_filter': department_filter,
        'departments': departments,
    }
    
    return render(request, 'businesscards/salesmen_list.html', context)


@login_required
def salesman_detail(request, slug):
    """View single salesman details"""
    card = get_object_or_404(SalesmanCard, slug=slug)
    
    context = {
        'card': card,
    }
    
    return render(request, 'businesscards/salesman_detail.html', context)


@login_required
def download_qr(request, slug):
    """Download QR code PNG"""
    card = get_object_or_404(SalesmanCard, slug=slug)
    
    # Get optional size parameter
    size = request.GET.get('size', None)
    if size:
        try:
            size = int(size)
            size = max(200, min(3000, size))  # Clamp between 200-3000
        except:
            size = None
    
    # Generate QR code with current settings
    qr_path = generate_qr_code(
        card, 
        request=request,
        size=size
    )
    
    if os.path.exists(qr_path):
        with open(qr_path, 'rb') as f:
            response = HttpResponse(f.read(), content_type='image/png')
            filename = f"{card.slug}-qr.png"
            if size:
                filename = f"{card.slug}-qr-{size}px.png"
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
    else:
        raise Http404("QR code not found")


@login_required
def customize_qr(request, slug):
    """Customize QR code design"""
    card = get_object_or_404(SalesmanCard, slug=slug)
    
    if request.method == 'POST':
        # Update QR settings
        card.qr_foreground_color = request.POST.get('qr_foreground_color', card.qr_foreground_color)
        card.qr_background_color = request.POST.get('qr_background_color', card.qr_background_color)
        card.qr_size = int(request.POST.get('qr_size', card.qr_size))
        card.qr_embed_logo = request.POST.get('qr_embed_logo') == 'on'
        card.qr_logo_size_percent = int(request.POST.get('qr_logo_size_percent', card.qr_logo_size_percent))
        card.qr_frame_style = request.POST.get('qr_frame_style', card.qr_frame_style)
        card.qr_frame_text = request.POST.get('qr_frame_text', '') or None
        card.qr_frame_text_color = request.POST.get('qr_frame_text_color', card.qr_frame_text_color)
        card.save()
        
        # Regenerate QR code
        try:
            generate_qr_code(card, request=request)
            messages.success(request, "QR code updated successfully!")
        except Exception as e:
            messages.error(request, f"Error generating QR code: {str(e)}")
        
        return redirect('businesscards:customize_qr', slug=card.slug)
    
    # Generate preview QR code
    qr_path = os.path.join(settings.MEDIA_ROOT, 'qr', f'{card.slug}.png')
    if not os.path.exists(qr_path):
        generate_qr_code(card, request=request)
    qr_url = card.get_qr_code_url()
    
    context = {
        'card': card,
        'qr_url': qr_url,
    }
    
    return render(request, 'businesscards/customize_qr.html', context)


@login_required
def add_salesman(request):
    """Add a new salesman card manually"""
    if request.method == 'POST':
        form = SalesmanCardForm(request.POST, request.FILES)
        if form.is_valid():
            card = form.save(commit=False)
            card.created_by = request.user
            card.save()
            
            # Generate QR code
            try:
                generate_qr_code(card, request=request)
            except Exception as e:
                messages.warning(request, f"Card created but QR code generation failed: {str(e)}")
            
            messages.success(request, f"Salesman card for {card.name} created successfully!")
            return redirect('businesscards:salesman_detail', slug=card.slug)
    else:
        form = SalesmanCardForm()
    
    context = {
        'form': form,
        'title': 'Add New Salesman',
        'action': 'Add'
    }
    
    return render(request, 'businesscards/salesman_form.html', context)


@login_required
def edit_salesman(request, slug):
    """Edit an existing salesman card"""
    card = get_object_or_404(SalesmanCard, slug=slug)
    
    if request.method == 'POST':
        form = SalesmanCardForm(request.POST, request.FILES, instance=card)
        if form.is_valid():
            card = form.save()
            
            # Regenerate QR code after update
            try:
                generate_qr_code(card, request=request)
            except Exception as e:
                messages.warning(request, f"Card updated but QR code regeneration failed: {str(e)}")
            
            messages.success(request, f"Salesman card for {card.name} updated successfully!")
            return redirect('businesscards:salesman_detail', slug=card.slug)
    else:
        form = SalesmanCardForm(instance=card)
    
    context = {
        'form': form,
        'card': card,
        'title': f'Edit {card.name}',
        'action': 'Update'
    }
    
    return render(request, 'businesscards/salesman_form.html', context)


@login_required
def delete_salesman(request, slug):
    """Delete a salesman card"""
    card = get_object_or_404(SalesmanCard, slug=slug)
    
    if request.method == 'POST':
        name = card.name
        card.delete()
        messages.success(request, f"Salesman card for {name} deleted successfully!")
        return redirect('businesscards:salesmen_list')
    
    context = {
        'card': card,
    }
    
    return render(request, 'businesscards/salesman_confirm_delete.html', context)


# Public views (no authentication required)

def card_public(request, slug):
    """Apple-style public digital card page"""
    card = get_object_or_404(SalesmanCard, slug=slug)
    
    context = {
        'card': card,
    }
    
    return render(request, 'businesscards/card_public.html', context)


def vcard_download(request, slug):
    """Download vCard file"""
    card = get_object_or_404(SalesmanCard, slug=slug)
    
    vcard_content = generate_vcard(card)
    
    response = HttpResponse(vcard_content, content_type='text/vcard')
    response['Content-Disposition'] = f'attachment; filename="{card.slug}.vcf"'
    
    return response
