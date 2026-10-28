import logging
import os
import re
from datetime import date as date_cls
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .datasheet import DatasheetError, to_jpeg
from .models import DEFAULT_VALIDITY, PAYMENT_TERM_CHOICES, Customer, Product, Quotation, QuotationItem, QuotationStamp, line_amount, new_share_token
from .permissions import can_use_stamp
from .salesman_lookup import salesman_for_user, search_salesmen
from .pdf_builder import build_quotation_pdf

log = logging.getLogger('quotation')

TEXT_FIELDS = [
    'customer_name', 'customer_contact', 'customer_person', 'customer_address', 'customer_trn', 'customer_code', 'customer_email', 'to_text', 'customer_ref', 'project_reference', 'remarks', 'validity', 'country_of_origin', 'warranty',
    'sales_person', 'email', 'contact_number', 'payment_terms_note',
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


def _save_with_next_number(quotation):
    """Save a new quotation under the next free JG-<year>-NNN number.

    If two people save at the same moment the unique constraint rejects the second one; it then simply takes the
    next number.
    """
    for _ in range(10):
        quotation.qtn_no = Quotation.next_qtn_no(quotation.date.year)
        try:
            with transaction.atomic():
                quotation.save()
            return
        except IntegrityError:
            quotation.pk = None
            quotation._state.adding = True
    raise IntegrityError('Could not allocate a quotation number.')


def _read_file(field_file):
    try:
        with field_file.open('rb') as fh:
            return fh.read()
    except (FileNotFoundError, ValueError):
        return None


def _copy_from_library(item, row):
    """A row filled from the product library gets its OWN copy of the product's photo and datasheet, so later changes
    to the library never alter this quotation, and its datasheet link (own token) keeps working.
    A file uploaded or removed on the row itself always wins over the library's."""
    lib = row.get('lib')
    if not lib:
        return
    if lib.photo and not row['photo_file'] and not row['remove_photo']:
        data = _read_file(lib.photo)
        if data is not None:
            if item.photo:
                item.photo.delete(save=False)
            item.photo.save(_display_name(lib.photo), ContentFile(data), save=False)
    if lib.datasheet and not ('ds_jpeg' in row and row['ds_jpeg']) and not row['remove_ds']:
        data = _read_file(lib.datasheet)
        if data is not None:
            if item.datasheet:
                item.datasheet.delete(save=False)
            item.datasheet.save(_display_name(lib.datasheet), ContentFile(data), save=False)
            item.share_token = item.share_token or new_share_token()


def _item_json(request, old, posted_row):
    """One row for the form's JS. `old` = the saved item (or None), `posted_row` = what was submitted."""
    def get(field):
        return posted_row[field] if posted_row else getattr(old, field)

    share_url = ''
    if old and old.datasheet and old.share_token:
        share_url = f'{_public_base_url(request)}{reverse("quotation:datasheet", args=[old.share_token])}'
    lib = posted_row.get('lib') if posted_row else None   # form re-shown after an error: keep the library choice
    return {
        'lib_id': lib.pk if lib else '',
        'lib_photo_url': reverse('quotation:product_photo', args=[lib.pk]) if lib and lib.photo else '',
        'lib_datasheet_name': _display_name(lib.datasheet) if lib and lib.datasheet else '',
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
    quotations = Quotation.objects.prefetch_related('items', 'emails')
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
        # The QTN No is never typed: a new quotation is numbered automatically when it is saved, and an existing one
        # keeps its number.
        try:
            qdate = date_cls.fromisoformat(posted.get('date') or '')
        except ValueError:
            qdate = None
            errors.append('Please enter a valid date.')
        stock_date = None
        subject_to_stock = posted.get('delivery_subject_to_stock') == 'on'   # then no date is kept
        if (posted.get('stock_availability') or '').strip() and not subject_to_stock:
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
        lib_ids = posted.getlist('lib_id')   # set when the row was filled from the product library
        library = Product.objects.in_bulk([int(x) for x in lib_ids if x.strip().isdigit()])
        rows = []
        for i, key in enumerate(keys):
            row = {
                'key': key,
                'item_id': _at(ids, i).strip(),
                'item_no': _at(item_nos, i).strip(),
                'model_no': _at(models_, i).strip(),
                'brand': _at(brands, i).strip().upper(), 'description': _at(descs, i).strip(),   # brand and color: capitals
                'size': _at(sizes, i).strip(), 'color': _at(colors, i).strip().upper(),
                'qty': _dec(_at(qtys, i, 0)), 'unit_price': _dec(_at(prices, i, 0)),
                'warranty': _at(warranties, i).strip(),
                'remove_photo': _at(rm_photo, i, '0') == '1',
                'remove_ds': _at(rm_ds, i, '0') == '1',
                'lib': library.get(int(_at(lib_ids, i).strip())) if _at(lib_ids, i).strip().isdigit() else None,
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
        discount_type = 'percent' if posted.get('discount_type') == 'percent' else 'amount'
        discount_value = _dec(posted.get('discount_value'))
        if discount_value < 0:
            errors.append('The discount cannot be negative.')
        elif discount_type == 'percent' and discount_value > 100:
            errors.append('A percentage discount cannot be more than 100%.')
        elif discount_type == 'amount' and discount_value > sum((line_amount(r['qty'], r['unit_price']) for r in rows), Decimal('0')):
            errors.append('The discount cannot be more than the total of the items.')

        if not errors:
            with transaction.atomic():
                is_new = quotation is None
                if is_new:
                    quotation = Quotation(created_by=request.user)
                quotation.date = qdate
                if can_use_stamp(request.user):   # everyone else leaves the existing choice untouched
                    quotation.add_stamp = posted.get('add_stamp') == 'on'
                quotation.stock_availability = stock_date
                quotation.delivery_subject_to_stock = subject_to_stock
                quotation.payment_terms = ', '.join(t for t in PAYMENT_TERM_CHOICES if t in posted.getlist('payment_terms'))
                quotation.discount_type = discount_type
                quotation.discount_value = discount_value
                for f in TEXT_FIELDS:
                    setattr(quotation, f, (posted.get(f) or '').strip())
                if is_new:
                    _save_with_next_number(quotation)
                else:
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
                    _copy_from_library(item, r)
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
        values = {k: posted.get(k, '') for k in ['date', 'stock_availability'] + TEXT_FIELDS}
        values['discount_type'] = 'percent' if posted.get('discount_type') == 'percent' else 'amount'
        values['discount_value'] = posted.get('discount_value', '')
        values['add_stamp'] = (posted.get('add_stamp') == 'on') if can_use_stamp(request.user) else bool(quotation and quotation.add_stamp)
        values['delivery_subject_to_stock'] = posted.get('delivery_subject_to_stock') == 'on'
        values['payment_terms'] = [t for t in PAYMENT_TERM_CHOICES if t in posted.getlist('payment_terms')]
    elif quotation:
        values = {k: getattr(quotation, k) for k in TEXT_FIELDS}
        values['add_stamp'] = quotation.add_stamp
        values['delivery_subject_to_stock'] = quotation.delivery_subject_to_stock
        values['payment_terms'] = [t.strip() for t in quotation.payment_terms.split(',') if t.strip()]
        values['discount_type'] = quotation.discount_type
        values['discount_value'] = quotation.discount_value or ''   # blank rather than 0.00
        values.update(date=quotation.date.isoformat(),
                      stock_availability=quotation.stock_availability.isoformat() if quotation.stock_availability else '')
    else:
        sm = salesman_for_user(request.user) or {}   # the matching Salesman (name, any case) supplies phone / email when filled in
        values = {'date': timezone.localdate().isoformat(), 'discount_type': 'amount', 'discount_value': '',
                  'validity': DEFAULT_VALIDITY, 'delivery_subject_to_stock': True, 'payment_terms': [],   # defaults for a new quotation only
                  'sales_person': sm.get('name') or request.user.get_full_name() or request.user.get_username(),
                  'email': sm.get('email') or request.user.email,
                  'contact_number': sm.get('phone', '')}

    if quotation:
        qtn_no_shown = quotation.qtn_no
    else:   # preview of the number this quotation will get (the year follows the quotation date)
        try:
            year = date_cls.fromisoformat(values.get('date') or '').year
        except ValueError:
            year = timezone.localdate().year
        qtn_no_shown = Quotation.next_qtn_no(year)

    return render(request, 'quotation/form.html', {
        'quotation': quotation,
        'qtn_no_shown': qtn_no_shown,
        'payment_term_choices': PAYMENT_TERM_CHOICES,
        'has_stamp': bool(QuotationStamp.get_instance().stamp),
        'can_stamp': can_use_stamp(request.user),
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
    with_stamp = can_use_stamp(request.user)
    pdf = build_quotation_pdf(quotation, _public_base_url(request), orientation, with_stamp=with_stamp)
    if with_stamp and quotation.add_stamp:
        log.info('Stamped quotation PDF generated: user=%s qtn=%s', request.user.username, quotation.qtn_no)
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


@login_required
@require_GET
def customer_search(request):
    """Customer-name suggestions from earlier quotations: each customer once, with the details of their latest quotation."""
    tokens = [t for t in re.split(r'[\s\-/]+', (request.GET.get('q') or '').strip()) if t]
    if not tokens:
        return JsonResponse({'results': []})
    rows = Quotation.objects.exclude(customer_name='')
    for t in tokens:   # every word typed must appear in the name, in any case
        rows = rows.filter(customer_name__icontains=t)
    latest = {}
    for name, contact, address, trn, qdate, email, code, person in rows.order_by('-date', '-id').values_list(
            'customer_name', 'customer_contact', 'customer_address', 'customer_trn', 'date', 'customer_email', 'customer_code', 'customer_person')[:500]:
        latest.setdefault(' '.join(name.lower().split()), {   # same name, any capitals/spacing = one customer (newest quotation wins)
            'name': ' '.join(name.split()), 'contact': contact, 'address': address, 'trn': trn, 'email': email, 'code': code, 'person': person,
            'date': qdate.strftime('%d %b %Y')})
    first = ' '.join(tokens).lower()
    # The Customer list is the master: a listed customer wins over the same name found in an older quotation.
    listed = Customer.objects.all()
    for t in tokens:
        listed = listed.filter(name__icontains=t)
    for c in listed.order_by('name')[:100]:
        latest[' '.join(c.name.lower().split())] = {
            'name': ' '.join(c.name.split()), 'contact': c.contact_number, 'person': c.contact_person, 'address': c.address,
            'trn': c.trn, 'email': c.email, 'code': c.code, 'date': f'ID {c.code}'}
    found = sorted(latest.items(), key=lambda kv: (not kv[0].startswith(first), kv[0]))
    return JsonResponse({'results': [v for _, v in found[:15]]})


@login_required
@require_GET
def salesman_search(request):
    """Salesman-name suggestions (read-only) with the phone and email stored for each."""
    return JsonResponse({'results': search_salesmen(request.GET.get('q'))})


MAX_STAMP_BYTES = 5 * 1024 * 1024


@login_required
def stamp_settings(request):
    """Upload / replace / remove the company stamp. The image is private: it is only ever printed into PDFs."""
    if not can_use_stamp(request.user):
        raise PermissionDenied
    stamp = QuotationStamp.get_instance()
    if request.method == 'POST':
        changed = False
        if request.POST.get('remove_stamp') == '1' and stamp.stamp:
            stamp.stamp.delete(save=False)
            changed = True
        f = request.FILES.get('stamp')
        if f:
            err = _check_photo(f, 'Stamp')
            if err:
                messages.error(request, err.replace('photo is too large', 'image is too large'))
            else:
                if stamp.stamp:
                    stamp.stamp.delete(save=False)
                stamp.stamp.save(f.name, f, save=False)
                changed = True
        if changed:
            stamp.save()
            log.info('Quotation stamp changed by user=%s', request.user.username)
            messages.success(request, 'Stamp updated.')
        return redirect('quotation:stamp_settings')
    return render(request, 'quotation/stamp_settings.html', {'stamp': stamp})


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
