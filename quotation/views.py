import logging
import os
from datetime import date as date_cls
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .datasheet import DatasheetError, to_jpeg
from .models import Quotation, QuotationItem, new_share_token
from .pdf_builder import build_quotation_pdf

log = logging.getLogger('quotation')

TEXT_FIELDS = [
    'to_text', 'project_reference', 'remarks', 'validity', 'country_of_origin',
    'sales_person', 'email', 'contact_number',
]
MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_DATASHEET_BYTES = 10 * 1024 * 1024


def _dec(value):
    try:
        return Decimal(str(value).replace(',', '').strip() or '0')
    except InvalidOperation:
        return Decimal('0')


def _at(values, i, default=''):
    return values[i] if i < len(values) else default


def _public_base_url(request):
    """Scheme + host used inside the datasheet links in the PDF.

    Set QUOTATION_PUBLIC_BASE_URL (env or settings) to the live https domain so the
    links are right even when a PDF is generated from another address.
    """
    configured = getattr(settings, 'QUOTATION_PUBLIC_BASE_URL', '') or os.getenv('QUOTATION_PUBLIC_BASE_URL', '')
    if configured:
        return configured.rstrip('/')
    scheme = request.headers.get('X-Forwarded-Proto', request.scheme).split(',')[0].strip()
    return f'{scheme}://{request.get_host()}'


def _check_photo(f, label):
    if f.size > MAX_PHOTO_BYTES:
        return f'{label}: photo is too large (max 5 MB).'
    try:
        from PIL import Image
        Image.open(f).verify()
        f.seek(0)
    except Exception:
        return f'{label}: please upload a valid image (PNG or JPG).'
    return None


def _display_name(field_file):
    base = os.path.basename(field_file.name)
    return base.split('_', 1)[1] if '_' in base else base


def _delete_item_files(item):
    if item.photo:
        item.photo.delete(save=False)
    if item.datasheet:
        item.datasheet.delete(save=False)


def _item_json(request, old, posted_row):
    """One row for the form's JS. `old` = the saved item (or None), `posted_row` = what was submitted."""
    def get(field):
        return posted_row[field] if posted_row else getattr(old, field)

    share_url = ''
    if old and old.datasheet and old.share_token:
        share_url = f'{_public_base_url(request)}{reverse("quotation:datasheet", args=[old.share_token])}'
    return {
        'item_id': old.pk if old else '',
        'item_no': get('item_no'), 'model_no': get('model_no'),
        'brand': get('brand'), 'description': get('description'), 'size': get('size'),
        'color': get('color'), 'qty': str(get('qty')), 'unit_price': str(get('unit_price')),
        'warranty': get('warranty'),
        'photo_url': reverse('quotation:photo', args=[old.pk]) + f'?v={int(old.quotation.updated_at.timestamp())}'
                     if old and old.photo else '',
        'datasheet_name': _display_name(old.datasheet) if old and old.datasheet else '',
        'share_url': share_url,
    }


@login_required
def quotation_list(request):
    q = (request.GET.get('q') or '').strip()
    quotations = Quotation.objects.prefetch_related('items')
    if q:
        quotations = quotations.filter(qtn_no__icontains=q)
    return render(request, 'quotation/list.html', {'quotations': quotations, 'q': q})


@login_required
def quotation_form(request, pk=None):
    quotation = get_object_or_404(Quotation, pk=pk) if pk else None
    existing = {str(i.pk): i for i in quotation.items.all()} if quotation else {}
    errors = []
    posted = None

    if request.method == 'POST':
        posted = request.POST
        qtn_no = (posted.get('qtn_no') or '').strip()
        if not qtn_no:
            errors.append('Please enter the QTN No.')
        elif Quotation.objects.filter(qtn_no__iexact=qtn_no).exclude(pk=quotation.pk if quotation else None).exists():
            errors.append(f'QTN No {qtn_no} already exists.')
        try:
            qdate = date_cls.fromisoformat(posted.get('date') or '')
        except ValueError:
            qdate = None
            errors.append('Please enter a valid date.')
        stock_date = None
        if (posted.get('stock_availability') or '').strip():
            try:
                stock_date = date_cls.fromisoformat(posted['stock_availability'].strip())
            except ValueError:
                errors.append('Please enter a valid Stock Availability date.')

        keys = posted.getlist('row_key')
        ids = posted.getlist('item_id')
        item_nos = posted.getlist('item_no')
        models_ = posted.getlist('item_model')
        brands = posted.getlist('item_brand')
        descs = posted.getlist('item_description')
        sizes = posted.getlist('item_size')
        colors = posted.getlist('item_color')
        qtys = posted.getlist('item_qty')
        prices = posted.getlist('item_price')
        warranties = posted.getlist('item_warranty')
        rm_photo = posted.getlist('remove_photo')
        rm_ds = posted.getlist('remove_ds')
        rows = []
        for i, key in enumerate(keys):
            row = {
                'key': key,
                'item_id': _at(ids, i).strip(),
                'item_no': _at(item_nos, i).strip(),
                'model_no': _at(models_, i).strip(),
                'brand': _at(brands, i).strip(), 'description': _at(descs, i).strip(),
                'size': _at(sizes, i).strip(), 'color': _at(colors, i).strip(),
                'qty': _dec(_at(qtys, i, 0)), 'unit_price': _dec(_at(prices, i, 0)),
                'warranty': _at(warranties, i).strip(),
                'remove_photo': _at(rm_photo, i, '0') == '1',
                'remove_ds': _at(rm_ds, i, '0') == '1',
                # one file input per row, named after the row key, so rows without a file can't shift the others
                'photo_file': request.FILES.get(f'photo_{key}'),
                'ds_file': request.FILES.get(f'ds_{key}'),
            }
            if not (row['brand'] or row['description'] or row['model_no']):
                continue
            n = len(rows) + 1
            if row['photo_file']:
                err = _check_photo(row['photo_file'], f'Item {n}')
                if err:
                    errors.append(err)
            row['ds_jpeg'] = None
            if row['ds_file']:
                if row['ds_file'].size > MAX_DATASHEET_BYTES:
                    errors.append(f'Item {n}: datasheet is too large (max 10 MB).')
                else:
                    try:   # PDF / image -> JPEG, so the shared link always shows a JPEG
                        row['ds_jpeg'] = to_jpeg(row['ds_file'])
                    except DatasheetError as e:
                        errors.append(f'Item {n}: {e}')
            rows.append(row)
        if not rows:
            errors.append('Please add at least one item.')

        if not errors:
            with transaction.atomic():
                if not quotation:
                    quotation = Quotation(created_by=request.user)
                quotation.qtn_no = qtn_no
                quotation.date = qdate
                quotation.stock_availability = stock_date
                for f in TEXT_FIELDS:
                    setattr(quotation, f, (posted.get(f) or '').strip())
                quotation.save()
                current = {str(i.pk): i for i in quotation.items.all()}
                for pos, r in enumerate(rows):
                    # Rows are updated in place (not recreated) so photos, datasheets and share tokens survive edits.
                    item = current.pop(r['item_id'], None) or QuotationItem(quotation=quotation)
                    item.position = pos
                    for f in ('item_no', 'model_no', 'brand', 'description', 'size', 'color', 'qty', 'unit_price', 'warranty'):
                        setattr(item, f, r[f])
                    if (r['remove_photo'] or r['photo_file']) and item.photo:
                        item.photo.delete(save=False)
                    if r['photo_file']:
                        item.photo.save(r['photo_file'].name, r['photo_file'], save=False)
                    if r['remove_ds'] and not r['ds_jpeg']:
                        if item.datasheet:
                            item.datasheet.delete(save=False)
                        item.share_token = None
                    if r['ds_jpeg']:
                        if item.datasheet:
                            item.datasheet.delete(save=False)   # same token: links already sent keep working
                        ds_name, ds_bytes = r['ds_jpeg']
                        item.datasheet.save(ds_name, ContentFile(ds_bytes), save=False)
                        item.share_token = item.share_token or new_share_token()
                    item.save()
                for gone in current.values():   # rows removed from the form
                    _delete_item_files(gone)
                    gone.delete()
            messages.success(request, f'Quotation {quotation.qtn_no} saved.')
            return redirect('quotation:list')

        items_data = [_item_json(request, existing.get(r['item_id']), r) for r in rows]
    else:
        items_data = [_item_json(request, it, None) for it in (quotation.items.all() if quotation else [])]

    if posted is not None:
        values = {k: posted.get(k, '') for k in ['qtn_no', 'date', 'stock_availability'] + TEXT_FIELDS}
    elif quotation:
        values = {k: getattr(quotation, k) for k in TEXT_FIELDS}
        values.update(qtn_no=quotation.qtn_no, date=quotation.date.isoformat(),
                      stock_availability=quotation.stock_availability.isoformat() if quotation.stock_availability else '')
    else:
        values = {'date': timezone.localdate().isoformat(),
                  'sales_person': request.user.get_full_name() or request.user.get_username(),
                  'email': request.user.email}

    return render(request, 'quotation/form.html', {
        'quotation': quotation,
        'errors': errors,
        'values': values,
        'items_data': items_data,
    })


@login_required
@require_POST
def quotation_delete(request, pk):
    quotation = get_object_or_404(Quotation, pk=pk)
    qtn_no = quotation.qtn_no
    for item in quotation.items.all():
        _delete_item_files(item)
    quotation.delete()
    messages.success(request, f'Quotation {qtn_no} deleted.')
    return redirect('quotation:list')


@login_required
def quotation_pdf(request, pk):
    quotation = get_object_or_404(Quotation.objects.prefetch_related('items'), pk=pk)
    orientation = 'landscape' if request.GET.get('orientation') == 'landscape' else 'portrait'
    pdf = build_quotation_pdf(quotation, _public_base_url(request), orientation)
    resp = HttpResponse(pdf, content_type='application/pdf')
    disp = 'inline' if request.GET.get('inline') == '1' else 'attachment'
    safe = ''.join(c if c.isalnum() or c in '-_' else '-' for c in quotation.qtn_no)
    suffix = '_Landscape' if orientation == 'landscape' else ''
    resp['Content-Disposition'] = f'{disp}; filename="Quotation_{safe}{suffix}.pdf"'
    resp['Cache-Control'] = 'no-store'
    return resp


@login_required
@require_GET
def item_photo(request, item_pk):
    """Staff-only preview of an item photo inside the form (the files are private)."""
    item = get_object_or_404(QuotationItem, pk=item_pk)
    if not item.photo:
        raise Http404
    resp = FileResponse(item.photo.open('rb'))
    resp['Cache-Control'] = 'private, max-age=3600'
    return resp


@require_GET
def datasheet_public(request, token):
    """No login: serves exactly one datasheet (a JPEG), looked up by its unguessable token."""
    item = QuotationItem.objects.filter(share_token=token).exclude(datasheet='').first()
    if not item or not item.datasheet:
        raise Http404
    try:
        fh = item.datasheet.open('rb')
    except FileNotFoundError:
        raise Http404
    ctype = 'application/pdf' if item.datasheet.name.lower().endswith('.pdf') else 'image/jpeg'
    resp = FileResponse(fh, content_type=ctype)
    # The link in the quotation PDF downloads the file; add ?view=1 to open it in the browser instead.
    disp = 'inline' if request.GET.get('view') == '1' else 'attachment'
    resp['Content-Disposition'] = f'{disp}; filename="{_display_name(item.datasheet)}"'
    resp['Cache-Control'] = 'no-store'
    resp['X-Robots-Tag'] = 'noindex, nofollow'
    return resp
