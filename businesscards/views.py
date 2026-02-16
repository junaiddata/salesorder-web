from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse, Http404
from django.db.models import Q
from django.core.paginator import Paginator
from .models import SalesmanCard
from .forms import ExcelUploadForm
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
    
    # Ensure QR code exists
    qr_path = os.path.join(settings.MEDIA_ROOT, 'qr', f'{card.slug}.png')
    
    if not os.path.exists(qr_path):
        # Generate QR code if it doesn't exist
        generate_qr_code(card, request=request)
    
    if os.path.exists(qr_path):
        with open(qr_path, 'rb') as f:
            response = HttpResponse(f.read(), content_type='image/png')
            response['Content-Disposition'] = f'attachment; filename="{card.slug}-qr.png"'
            return response
    else:
        raise Http404("QR code not found")


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
