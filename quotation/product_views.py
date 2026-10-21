"""Product library: add products once (model no, brand, description, photo, datasheet), then pick them in a quotation."""
import logging
import os
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .datasheet import DatasheetError, to_jpeg
from .models import Product, product_key
from .views import MAX_DATASHEET_BYTES, _check_photo, _display_name

log = logging.getLogger('quotation')

MAX_EXCEL_BYTES = 5 * 1024 * 1024
MAX_EXCEL_ROWS = 5000
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), 'sample_files', 'product_library_template.xlsx')
HEADERS = {
    'model no': 'model_no', 'model': 'model_no', 'model number': 'model_no',
    'brand': 'brand', 'description': 'description',
    'photo file': 'photo_name', 'datasheet file': 'datasheet_name',
}


def _delete_files(product):
    if product.photo:
        product.photo.delete(save=False)
    if product.datasheet:
        product.datasheet.delete(save=False)


def _set_photo(product, f):
    if product.photo:
        product.photo.delete(save=False)
    product.photo.save(f.name, f, save=False)


def _set_datasheet(product, name, data):
    from django.core.files.base import ContentFile
    if product.datasheet:
        product.datasheet.delete(save=False)
    product.datasheet.save(name, ContentFile(data), save=False)


@login_required
def product_list(request):
    q = (request.GET.get('q') or '').strip()
    products = Product.objects.all()
    if q:
        products = products.filter(Q(model_key__icontains=product_key(q)) | Q(brand__icontains=q) | Q(description__icontains=q))
    page = Paginator(products, 50).get_page(request.GET.get('page'))
    return render(request, 'quotation/product_list.html', {
        'page': page, 'q': q, 'total': Product.objects.count(),
        # lets the page check file names against the library before anything is uploaded
        'keys': {p.model_key: p.model_no for p in Product.objects.only('model_key', 'model_no')},
        'hints': {'photo': {product_key(p.photo_name): p.model_no for p in Product.objects.exclude(photo_name='')},
                  'datasheet': {product_key(p.datasheet_name): p.model_no for p in Product.objects.exclude(datasheet_name='')}},
    })


@login_required
def product_form(request, pk=None):
    product = get_object_or_404(Product, pk=pk) if pk else None
    errors = []
    values = {'model_no': product.model_no if product else '', 'brand': product.brand if product else '',
              'description': product.description if product else ''}
    if request.method == 'POST':
        values = {k: (request.POST.get(k) or '').strip() for k in ('model_no', 'brand', 'description')}
        values['brand'] = values['brand'].upper()
        photo, ds = request.FILES.get('photo'), request.FILES.get('datasheet')
        if not values['model_no']:
            errors.append('Model No is required.')
        elif Product.objects.filter(model_key=product_key(values['model_no'])).exclude(pk=product.pk if product else None).exists():
            errors.append(f'Model No "{values["model_no"]}" already exists in the library.')
        if photo:
            err = _check_photo(photo, 'Photo')
            if err:
                errors.append(err)
        ds_jpeg = None
        if ds:
            if ds.size > MAX_DATASHEET_BYTES:
                errors.append('Datasheet is too large (max 10 MB).')
            else:
                try:
                    ds_jpeg = to_jpeg(ds)
                except DatasheetError as e:
                    errors.append(f'Datasheet: {e}')
        if not errors:
            product = product or Product()
            product.model_no, product.brand, product.description = values['model_no'], values['brand'], values['description']
            if request.POST.get('remove_photo') == '1' and product.photo:
                product.photo.delete(save=False)
            if photo:
                _set_photo(product, photo)
            if request.POST.get('remove_ds') == '1' and product.datasheet and not ds_jpeg:
                product.datasheet.delete(save=False)
            if ds_jpeg:
                _set_datasheet(product, *ds_jpeg)
            product.save()
            messages.success(request, f'Product {product.model_no} saved.')
            return redirect('quotation:product_list')
    return render(request, 'quotation/product_form.html', {'product': product, 'values': values, 'errors': errors})


@login_required
@require_POST
def product_delete(request, pk):
    product = get_object_or_404(Product, pk=pk)
    name = product.model_no
    _delete_files(product)
    product.delete()
    messages.success(request, f'Product {name} deleted. Quotations that already used it are not changed.')
    return redirect('quotation:product_list')


@login_required
@require_GET
def product_photo(request, pk):
    """Login-only preview of a library photo (the files are private)."""
    product = get_object_or_404(Product, pk=pk)
    if not product.photo:
        raise Http404
    try:
        resp = FileResponse(product.photo.open('rb'))
    except FileNotFoundError:
        raise Http404
    resp['Cache-Control'] = 'private, max-age=300'
    return resp


@login_required
@require_GET
def product_search(request):
    """Model No suggestions for the quotation form."""
    q = (request.GET.get('q') or '').strip()
    if not q:
        return JsonResponse({'results': []})
    key = product_key(q)
    products = list(Product.objects.filter(Q(model_key__icontains=key) | Q(brand__icontains=q))
                    .order_by('model_key')[:40])
    products.sort(key=lambda p: (not p.model_key.startswith(key), p.model_key))   # models starting with the typed text first
    return JsonResponse({'results': [{
        'id': p.pk, 'model_no': p.model_no, 'brand': p.brand, 'description': p.description,
        'photo_url': reverse('quotation:product_photo', args=[p.pk]) + f'?v={int(p.updated_at.timestamp())}' if p.photo else '',
        'datasheet_name': _display_name(p.datasheet) if p.datasheet else '',
    } for p in products[:15]]})


def _cell(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


@login_required
@require_POST
def product_import_excel(request):
    """Step 1: read the Excel template and create / update products (text only; files come in step 2)."""
    f = request.FILES.get('excel')
    if not f:
        messages.error(request, 'Please choose the Excel file.')
        return redirect('quotation:product_list')
    if f.size > MAX_EXCEL_BYTES or not f.name.lower().endswith('.xlsx'):
        messages.error(request, 'Please upload an .xlsx Excel file (max 5 MB).')
        return redirect('quotation:product_list')
    from openpyxl import load_workbook
    try:
        sheet = load_workbook(f, read_only=True, data_only=True).worksheets[0]
        rows = list(sheet.iter_rows(values_only=True, max_row=MAX_EXCEL_ROWS + 1))
    except Exception:
        messages.error(request, 'This file could not be read. Please use the downloaded template (.xlsx).')
        return redirect('quotation:product_list')
    cols = {}
    for i, h in enumerate(rows[0] if rows else ()):
        field = HEADERS.get(_cell(h).lower())
        if field and field not in cols:
            cols[field] = i
    if 'model_no' not in cols:
        messages.error(request, 'The first row must have a "Model No" column. Please use the downloaded template.')
        return redirect('quotation:product_list')

    created = updated = 0
    problems = []
    seen = set()
    for n, row in enumerate(rows[1:], start=2):
        data = {k: _cell(row[i]) if i < len(row) else '' for k, i in cols.items()}
        model_no = data.get('model_no', '')
        if not model_no:
            if any(data.values()):
                problems.append(f'Row {n}: Model No is empty, skipped.')
            continue
        if len(model_no) > 100:
            problems.append(f'Row {n}: Model No is too long, skipped.')
            continue
        key = product_key(model_no)
        if key in seen:
            problems.append(f'Row {n}: {model_no} is repeated in the sheet, skipped.')
            continue
        seen.add(key)
        product = Product.objects.filter(model_key=key).first()
        is_new = product is None
        product = product or Product(model_no=model_no)
        # a blank cell never wipes what is already in the library
        if data.get('brand'):
            product.brand = data['brand'].upper()[:150]
        if data.get('description'):
            product.description = data['description']
        if data.get('photo_name'):
            product.photo_name = data['photo_name'][:200]
        if data.get('datasheet_name'):
            product.datasheet_name = data['datasheet_name'][:200]
        product.save()
        created += is_new
        updated += not is_new
    messages.success(request, f'Excel imported: {created} new, {updated} updated. Now upload the photos and datasheets below.')
    for p in problems[:10]:
        messages.warning(request, p)
    if len(problems) > 10:
        messages.warning(request, f'…and {len(problems) - 10} more rows skipped.')
    return redirect('quotation:product_list')


@login_required
@require_POST
def product_import_files(request):
    """Step 2: photos or datasheets, matched to products by file name. Called in small batches by the page; returns JSON."""
    kind = request.POST.get('kind')
    if kind not in ('photo', 'datasheet'):
        return JsonResponse({'error': 'bad request'}, status=400)
    hint_field = 'photo_name' if kind == 'photo' else 'datasheet_name'
    hints = {product_key(getattr(p, hint_field)): p for p in Product.objects.exclude(**{hint_field: ''})}
    results = []
    for f in request.FILES.getlist('files'):
        stem = re.sub(r'\.[^.]*$', '', os.path.basename(f.name))
        product = hints.get(product_key(os.path.basename(f.name))) or Product.objects.filter(model_key=product_key(stem)).first()
        res = {'name': f.name, 'status': 'unmatched', 'model_no': '', 'message': 'No product with this Model No.'}
        if product:
            res['model_no'] = product.model_no
            try:
                if kind == 'photo':
                    err = _check_photo(f, 'Photo')
                    if err:
                        raise ValueError(err)
                    _set_photo(product, f)
                else:
                    if f.size > MAX_DATASHEET_BYTES:
                        raise ValueError('Datasheet is too large (max 10 MB).')
                    _set_datasheet(product, *to_jpeg(f))
                product.save()
                res.update(status='ok', message='Saved.')
            except (ValueError, DatasheetError) as e:
                res.update(status='error', message=str(e))
            except Exception:
                log.exception('Product library file import failed: %s', f.name)
                res.update(status='error', message='Could not save this file.')
        results.append(res)
    return JsonResponse({'results': results})


@login_required
@require_GET
def product_template(request):
    if not os.path.exists(TEMPLATE_PATH):
        raise Http404
    return FileResponse(open(TEMPLATE_PATH, 'rb'), as_attachment=True, filename='product_library_template.xlsx')
