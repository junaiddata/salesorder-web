"""
SAP Quotation Views - API Sync functionality
"""
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from datetime import datetime
from decimal import Decimal
import pandas as pd
import json
import logging

logger = logging.getLogger(__name__)

# Import models
from .models import SAPQuotation, SAPQuotationItem
from .api_client import SAPAPIClient
from .sync_services import sync_quotations_core


@login_required
def sync_quotations_from_api(request):
    """Sync quotations from SAP API"""
    messages_list = []
    sync_stats = {
        'created': 0,
        'updated': 0,
        'closed': 0,
        'total_quotations': 0,
        'total_items': 0,
        'api_calls': 0,
        'errors': []
    }

    if request.method == 'POST':
        days_back = int(request.POST.get('days_back', getattr(settings, 'SAP_SYNC_DAYS_BACK', 3)))
        sync_stats = sync_quotations_core(days_back=days_back)

        if sync_stats['errors']:
            messages.error(request, f"Error syncing from API: {sync_stats['errors'][-1]}")
        elif sync_stats['total_quotations'] == 0:
            messages.warning(request, "No quotations found from API.")
        else:
            messages.success(
                request,
                f"Synced {sync_stats['total_quotations']} quotations: "
                f"{sync_stats['created']} created, {sync_stats['updated']} updated, "
                f"{sync_stats['closed']} closed. Total items: {sync_stats['total_items']}."
            )
            return redirect('quotation_list')

    return render(request, 'quotes/upload_quotations.html', {
        'messages': messages_list,
        'sync_stats': sync_stats
    })


# =====================
# Quotation: API Sync Receive (from PC script)
# =====================
@csrf_exempt
@require_POST
def sync_quotations_api_receive(request):
    """
    Receive quotations data from PC script via HTTP API
    This endpoint is called by the PC sync script
    """
    try:
        # Get data from request (JSON)
        if request.content_type and 'application/json' in request.content_type:
            data = json.loads(request.body)
        else:
            # Try to parse as JSON anyway
            try:
                data = json.loads(request.body)
            except:
                data = request.POST.dict()
        
        # Verify API key
        api_key = data.get('api_key')
        expected_key = getattr(settings, 'VPS_API_KEY', 'your-secret-api-key')
        
        if not api_key or api_key != expected_key:
            return JsonResponse({
                'success': False,
                'error': 'Invalid API key'
            }, status=401)
        
        quotations = data.get('quotations', [])
        api_q_numbers = data.get('api_q_numbers', [])
        
        if not quotations:
            return JsonResponse({
                'success': False,
                'error': 'No quotations provided'
            })
        
        # Process quotations (reuse existing sync logic)
        stats = {
            'created': 0,
            'updated': 0,
            'closed': 0,
            'total_items': 0
        }
        
        q_numbers = [m['q_number'] for m in quotations if m.get('q_number')]
        api_q_numbers_set = set(api_q_numbers)
        
        with transaction.atomic():
            # Fetch existing quotations
            try:
                existing_map = {q.q_number: q for q in SAPQuotation.objects.filter(q_number__in=q_numbers)}
            except Exception:
                existing_map = {}
            
            to_create = []
            to_update = []
            
            def _dec2(x) -> Decimal:
                try:
                    if x is None or (isinstance(x, float) and pd.isna(x)):
                        return Decimal("0.00")
                    return Decimal(str(x)).quantize(Decimal("0.01"))
                except Exception:
                    return Decimal("0.00")
            
            # Process each mapped quotation
            for mapped in quotations:
                q_no = mapped.get('q_number')
                if not q_no:
                    continue
                
                # Parse posting_date if it's a string
                posting_date = mapped.get('posting_date')
                if isinstance(posting_date, str):
                    try:
                        posting_date = datetime.strptime(posting_date, '%Y-%m-%d').date()
                    except (ValueError, TypeError):
                        posting_date = None
                elif posting_date and hasattr(posting_date, 'date'):
                    posting_date = posting_date.date() if hasattr(posting_date, 'date') else posting_date
                
                defaults = {
                    "posting_date": posting_date,
                    "customer_code": mapped.get('customer_code', ''),
                    "customer_name": mapped.get('customer_name', ''),
                    "bp_reference_no": mapped.get('bp_reference_no', ''),
                    "salesman_name": mapped.get('salesman_name', ''),
                    "document_total": _dec2(mapped.get('document_total', 0)),
                    "vat_sum": _dec2(mapped.get('vat_sum', 0)),
                    "total_discount": _dec2(mapped.get('total_discount', 0)),
                    "rounding_diff_amount": _dec2(mapped.get('rounding_diff_amount', 0)),
                    "discount_percent": _dec2(mapped.get('discount_percent', 0)),
                    "status": mapped.get('status', 'CLOSED'),
                    "bill_to": mapped.get('bill_to', '') or '',
                    "remarks": mapped.get('remarks', '') or '',
                }
                
                if mapped.get('internal_number'):
                    defaults["internal_number"] = mapped.get('internal_number')
                
                obj = existing_map.get(q_no)
                if obj is None:
                    to_create.append(SAPQuotation(q_number=q_no, **defaults))
                    stats['created'] += 1
                else:
                    for k, v in defaults.items():
                        setattr(obj, k, v)
                    to_update.append(obj)
                    stats['updated'] += 1
            
            # Bulk create/update
            if to_create:
                SAPQuotation.objects.bulk_create(to_create, batch_size=5000)
            
            if to_update:
                update_fields = [
                    "posting_date", "customer_code", "customer_name", "bp_reference_no",
                    "salesman_name", "document_total", "vat_sum", "total_discount",
                    "rounding_diff_amount", "discount_percent", "status", "bill_to",
                    "remarks", "internal_number"
                ]
                SAPQuotation.objects.bulk_update(to_update, fields=update_fields, batch_size=5000)
            
            # Re-fetch ids for FK mapping
            quotation_id_map = dict(
                SAPQuotation.objects.filter(q_number__in=q_numbers).values_list("q_number", "id")
            )
            
            # Delete existing items for these quotations
            SAPQuotationItem.objects.filter(quotation__q_number__in=q_numbers).delete()
            
            # Build items list + bulk insert
            items_to_create = []
            
            def _dec_any(x) -> Decimal:
                try:
                    if x is None or (isinstance(x, float) and pd.isna(x)):
                        return Decimal("0")
                    return Decimal(str(x))
                except Exception:
                    return Decimal("0")
            
            for mapped in quotations:
                q_no = mapped.get('q_number')
                q_id = quotation_id_map.get(q_no)
                if not q_id:
                    continue
                
                for item_data in mapped.get('items', []):
                    items_to_create.append(
                        SAPQuotationItem(
                            quotation_id=q_id,
                            item_no=item_data.get('item_no', ''),
                            description=item_data.get('description', ''),
                            quantity=_dec_any(item_data.get('quantity', 0)),
                            price=_dec_any(item_data.get('price', 0)),
                            row_total=_dec_any(item_data.get('row_total', 0)),
                        )
                    )
                    
                    if len(items_to_create) >= 20000:
                        SAPQuotationItem.objects.bulk_create(items_to_create, batch_size=20000)
                        items_to_create = []
            
            if items_to_create:
                SAPQuotationItem.objects.bulk_create(items_to_create, batch_size=20000)
            
            stats['total_items'] = sum(len(m.get('items', [])) for m in quotations)
            
            # Close missing quotations
            previously_open_quotations = SAPQuotation.objects.filter(
                status__in=['O', 'OPEN', 'Open', 'open'],
                q_number__isnull=False
            ).exclude(q_number__in=api_q_numbers_set)
            
            closed_count = 0
            for quotation in previously_open_quotations:
                quotation.status = 'CLOSED'
                quotation.save(update_fields=['status'])
                closed_count += 1
            
            stats['closed'] = closed_count
            
            return JsonResponse({
                'success': True,
                'stats': stats
            })
            
    except Exception as e:
        logger.exception('Error in sync_quotations_api_receive')
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=500)
