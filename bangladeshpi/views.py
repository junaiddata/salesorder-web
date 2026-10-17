import logging
from datetime import date as date_cls
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from so.models import Items
from .models import REF_PREFIX, BangladeshPI, BangladeshPIItem, BangladeshPISeal
from .excel_builder import build_pi_xlsx
from .pdf_builder import build_pi_pdf
from .permissions import can_use_seal

audit_log = logging.getLogger('bangladeshpi.audit')

TEXT_FIELDS = [
    'port_of_origin', 'country_of_origin', 'transport', 'insurance',
    'destination', 'final_destination', 'shipment', 'validity', 'payment',
    'freight_valid_till', 'bank_terms_text', 'bank_payment', 'bank_insurance',
]

# Pre-filled on a NEW PI only; all editable. Insurance applies to both copies
# (the bank copy falls back to it when its own Insurance box is blank).
DEFAULT_PAYMENT = 'IRREVOCABLE & CONFIRMED L/C AT SIGHT & BALANCE ADVANCE T/T'
DEFAULT_INSURANCE = 'BY THE BUYER'
DEFAULT_BANK_PAYMENT = 'IRREVOCABLE & CONFIRMED L/C AT SIGHT'

DEFAULT_BANK_TERMS = (
    'THE LATEST SHIPPING DATE MUST BE AT LEAST 2 MONTHS FROM THE DATE OF ISSUE.\n'
    'THE EXPIRY DATE MUST BE AT LEAST 15 DAYS FROM THE LATEST SHIPPING DATE.\n'
    'THE L/C MUST BE CONFIRMED BY AN DUBAI BANK UNLESS ISSUED BY AN INTERNATIONAL/ FIRST CLASS BANK.\n'
    'CONFIRMATION FEES FOR BENEFICIARY ACCOUNT.\n'
    'PARTIAL AND TRANSHIPMENT TO BE ALLOWED'
)


def _dec_or_none(value):
    value = str(value or '').replace(',', '').strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _dec(value):
    try:
        return Decimal(str(value).replace(',', '').strip() or '0')
    except InvalidOperation:
        return Decimal('0')


SEARCH_LIMIT = 50


@login_required
def items_search(request):
    """Read-only catalog search for the PI item rows.

    Every word typed must match the item code, UPC or description (any order).
    Returns the total match count so the UI can warn when results are cut.
    """
    terms = (request.GET.get('q') or '').split()
    brand = (request.GET.get('brand') or '').strip()
    if not terms or sum(len(t) for t in terms) < 2:
        return JsonResponse({'results': [], 'total': 0})

    qs = Items.objects.all()
    if brand:
        qs = qs.filter(item_firm=brand)
    for t in terms:
        qs = qs.filter(Q(item_code__icontains=t) | Q(item_upvc__icontains=t) | Q(item_description__icontains=t))
    qs = qs.order_by('item_description', 'item_code')
    total = qs.count()
    results = [
        {'id': i.id, 'upc': i.item_upvc or '', 'item_code': i.item_code,
         'description': i.item_description, 'brand': i.item_firm}
        for i in qs[:SEARCH_LIMIT]
    ]
    return JsonResponse({'results': results, 'total': total})


@login_required
def pi_list(request):
    q = (request.GET.get('q') or '').strip()
    pis = BangladeshPI.objects.prefetch_related('items')
    if q:
        pis = pis.filter(ref_suffix__icontains=q)
    return render(request, 'bangladeshpi/pi_list.html', {'pis': pis, 'q': q})


@login_required
def pi_form(request, pk=None):
    pi = get_object_or_404(BangladeshPI, pk=pk) if pk else None
    errors = []
    posted = None

    if request.method == 'POST':
        posted = request.POST
        suffix = (posted.get('ref_suffix') or '').strip().strip('/')
        if not suffix:
            errors.append('Please enter the Ref No number.')
        elif BangladeshPI.objects.filter(ref_suffix__iexact=suffix).exclude(pk=pi.pk if pi else None).exists():
            errors.append(f'Ref No {REF_PREFIX}{suffix} already exists.')
        try:
            pi_date = date_cls.fromisoformat(posted.get('date') or '')
        except ValueError:
            pi_date = None
            errors.append('Please enter a valid date.')

        kinds = posted.getlist('item_kind')
        codes = posted.getlist('item_code')
        descs = posted.getlist('item_description')
        qtys = posted.getlist('item_qty')
        prices = posted.getlist('item_price')
        bank_prices = posted.getlist('item_bank_price')
        focs = posted.getlist('item_foc')
        rows = []
        for i, kind in enumerate(kinds):
            code = (codes[i] if i < len(codes) else '').strip()
            desc = (descs[i] if i < len(descs) else '').strip()
            if not code and not desc:
                continue
            is_foc = kind != 'heading' and (focs[i] if i < len(focs) else '0') == '1'
            rows.append({
                'kind': 'heading' if kind == 'heading' else 'item',
                'code': code, 'description': desc,
                'qty': _dec(qtys[i] if i < len(qtys) else 0),
                # a FOC row carries no prices at all
                'unit_price': Decimal('0') if is_foc else _dec(prices[i] if i < len(prices) else 0),
                'bank_unit_price': (
                    None if is_foc else _dec_or_none(bank_prices[i] if i < len(bank_prices) else '')
                ) if kind != 'heading' else None,
                'is_foc': is_foc,
            })
        item_rows = [r for r in rows if r['kind'] == 'item']
        if not item_rows:
            errors.append('Please add at least one item.')
        # Bank copy is all-or-nothing: either no bank prices, or one for every item.
        priced_rows = [r for r in item_rows if not r['is_foc']]
        n_bank = sum(1 for r in priced_rows if r['bank_unit_price'] is not None)
        if 0 < n_bank < len(priced_rows):
            errors.append('Bank copy: please enter a Bank Unit Price for every item, or clear them all.')

        if not errors:
            with transaction.atomic():
                if not pi:
                    pi = BangladeshPI(created_by=request.user)
                pi.ref_suffix = suffix
                pi.date = pi_date
                pi.freight_charges = _dec(posted.get('freight_charges'))
                if can_use_seal(request.user):   # everyone else leaves the existing choice untouched
                    pi.add_signature = posted.get('add_signature') == 'on'
                    pi.add_stamp = posted.get('add_stamp') == 'on'
                for f in TEXT_FIELDS:
                    setattr(pi, f, (posted.get(f) or '').strip())
                pi.save()
                pi.items.all().delete()
                BangladeshPIItem.objects.bulk_create([
                    BangladeshPIItem(pi=pi, position=n, **r) for n, r in enumerate(rows)
                ])
            messages.success(request, f'Proforma Invoice {pi.ref_no} saved.')
            return redirect('bangladeshpi:list')

        items_data = rows
    else:
        items_data = [
            {'kind': it.kind, 'code': it.code, 'description': it.description,
             'qty': it.qty, 'unit_price': it.unit_price, 'bank_unit_price': it.bank_unit_price,
             'is_foc': it.is_foc}
            for it in (pi.items.all() if pi else [])
        ]

    if posted is not None:
        values = {k: posted.get(k, '') for k in ['ref_suffix', 'date', 'freight_charges'] + TEXT_FIELDS}
        values.update(add_signature=posted.get('add_signature') == 'on', add_stamp=posted.get('add_stamp') == 'on')
        if not can_use_seal(request.user) and pi:
            values.update(add_signature=pi.add_signature, add_stamp=pi.add_stamp)
    elif pi:
        values = {k: getattr(pi, k) for k in TEXT_FIELDS}
        values.update(ref_suffix=pi.ref_suffix, date=pi.date.isoformat(), freight_charges=pi.freight_charges,
                      add_signature=pi.add_signature, add_stamp=pi.add_stamp)
    else:
        values = {'date': timezone.localdate().isoformat(), 'freight_charges': '0',
                  'bank_terms_text': DEFAULT_BANK_TERMS,
                  'payment': DEFAULT_PAYMENT, 'insurance': DEFAULT_INSURANCE,
                  'bank_payment': DEFAULT_BANK_PAYMENT}

    return render(request, 'bangladeshpi/pi_form.html', {
        'pi': pi,
        'ref_prefix': REF_PREFIX,
        'errors': errors,
        'values': values,
        'items_data': items_data,
        'seal': BangladeshPISeal.get_instance(),
        'can_seal': can_use_seal(request.user),
        'brands': list(Items.objects.order_by('item_firm').values_list('item_firm', flat=True).distinct()),
    })


@login_required
@require_POST
def pi_delete(request, pk):
    pi = get_object_or_404(BangladeshPI, pk=pk)
    ref = pi.ref_no
    pi.delete()
    messages.success(request, f'Proforma Invoice {ref} deleted.')
    return redirect('bangladeshpi:list')


@login_required
def pi_pdf(request, pk):
    pi = get_object_or_404(BangladeshPI.objects.prefetch_related('items'), pk=pk)
    variant = 'bank' if request.GET.get('copy') == 'bank' else 'customer'
    if variant == 'bank' and not pi.has_bank_prices:
        messages.error(request, 'Add a Bank Unit Price for every item (Edit PI) to enable the Bank PDF.')
        return redirect('bangladeshpi:list')
    with_seals = can_use_seal(request.user)
    pdf = build_pi_pdf(pi, variant, with_seals=with_seals)
    if with_seals and (pi.add_signature or pi.add_stamp):
        audit_log.info('Signed PDF generated: user=%s pi=%s copy=%s signature=%s stamp=%s',
                       request.user.username, pi.ref_no, variant, pi.add_signature, pi.add_stamp)
    resp = HttpResponse(pdf, content_type='application/pdf')
    disp = 'inline' if request.GET.get('inline') == '1' else 'attachment'
    resp['Content-Disposition'] = (
        f'{disp}; filename="PI_{pi.ref_no.replace("/", "-")}_{variant.capitalize()}.pdf"'
    )
    resp['Cache-Control'] = 'no-store'
    return resp


@login_required
def pi_excel(request, pk):
    """Excel version of the customer or bank copy (same data as the PDF)."""
    pi = get_object_or_404(BangladeshPI.objects.prefetch_related('items'), pk=pk)
    variant = 'bank' if request.GET.get('copy') == 'bank' else 'customer'
    if variant == 'bank' and not pi.has_bank_prices:
        messages.error(request, 'Add a Bank Unit Price for every item (Edit PI) to enable the Bank Excel.')
        return redirect('bangladeshpi:list')
    resp = HttpResponse(
        build_pi_xlsx(pi, variant),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    resp['Content-Disposition'] = f'attachment; filename="PI_{pi.ref_no.replace("/", "-")}_{variant.capitalize()}.xlsx"'
    resp['Cache-Control'] = 'no-store'
    return resp


MAX_SEAL_BYTES = 5 * 1024 * 1024


@login_required
def seal_settings(request):
    """Upload / replace / remove the company signature and stamp images."""
    if not can_use_seal(request.user):
        raise PermissionDenied
    seal = BangladeshPISeal.get_instance()
    if request.method == 'POST':
        changed = False
        for field, label in (('signature', 'Signature'), ('stamp', 'Stamp')):
            if request.POST.get(f'remove_{field}') == '1' and getattr(seal, field):
                getattr(seal, field).delete(save=False)
                changed = True
            f = request.FILES.get(field)
            if f:
                if f.size > MAX_SEAL_BYTES:
                    messages.error(request, f'{label} image is too large (max 5 MB).')
                    continue
                try:
                    from PIL import Image
                    Image.open(f).verify()
                    f.seek(0)
                except Exception:
                    messages.error(request, f'{label}: please upload a valid image (PNG or JPG).')
                    continue
                if getattr(seal, field):
                    getattr(seal, field).delete(save=False)
                getattr(seal, field).save(f.name, f, save=False)
                changed = True
        if changed:
            seal.save()
            audit_log.info('Signature/stamp changed by user=%s', request.user.username)
            messages.success(request, 'Signature & stamp updated.')
        return redirect('bangladeshpi:seal_settings')
    return render(request, 'bangladeshpi/seal_settings.html', {'seal': seal})
