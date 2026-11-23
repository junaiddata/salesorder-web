"""Commercial invoice views (converted from a PI's bank copy). Separate from views.py."""
import re
from datetime import date as date_cls

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from .commercial_pdf import DEFAULT_HS_CODE, build_commercial_invoice_pdf
from .models import (
    BangladeshCommercialInvoice, BangladeshCommercialInvoiceLine, BangladeshPI, BangladeshPISeal,
)
from .packing_pdf import build_packing_list_pdf, packing_list_no
from .permissions import can_use_seal
from .views import _dec_or_none, audit_log


def _hs_from_pi(pi):
    """HS code typed in the PI's heading row, e.g. '(HS CODE NO. - 8516.10.90)'."""
    for it in pi.items.all():
        if it.kind == 'heading':
            m = re.search(r'HS\s*CODE\s*NO\.?\s*[-:]?\s*([\d.]+)', it.description or '', re.I)
            if m and m.group(1).strip('.'):
                return m.group(1).strip('.')
    return DEFAULT_HS_CODE


def _fill_from_pi(ci, pi):
    """Copy everything the invoice takes from the PI (a snapshot). Weights already typed are kept."""
    old = {(l.code, l.description): l for l in ci.lines.all()} if ci.pk else {}
    ci.invoice_no = pi.ref_no
    ci.pi_ref_no = pi.ref_no
    ci.pi_date = pi.date
    ci.country_of_origin = pi.country_of_origin
    ci.hs_code = _hs_from_pi(pi)
    ci.freight_charges = pi.bank_freight_charges or 0
    ci.save()
    ci.lines.all().delete()
    new = []
    pos = 0
    for it in pi.items.all():
        if it.kind != 'item' or it.is_foc:
            continue      # no product heading row on the invoice; FOC items are not on the bank copy
        prev = old.get((it.code, it.description))
        new.append(BangladeshCommercialInvoiceLine(
            ci=ci, position=pos, kind=it.kind, code=it.code, description=it.description,
            qty=it.qty, unit_price=it.bank_unit_price or 0,
            pack=prev.pack if prev else None, gross_kg=prev.gross_kg if prev else None,
            net_kg=prev.net_kg if prev else None,
        ))
        pos += 1
    BangladeshCommercialInvoiceLine.objects.bulk_create(new)


def _date_or_none(value):
    try:
        return date_cls.fromisoformat((value or '').strip())
    except ValueError:
        return None


@login_required
def ci_edit(request, pk):
    pi = get_object_or_404(BangladeshPI.objects.prefetch_related('items'), pk=pk)
    if not pi.has_bank_prices:
        messages.error(request, 'Add bank prices to this PI (Edit PI) before making a commercial invoice.')
        return redirect('bangladeshpi:list')

    ci = BangladeshCommercialInvoice.objects.filter(pi=pi).first()
    if ci is None:
        with transaction.atomic():
            ci = BangladeshCommercialInvoice(
                pi=pi, created_by=request.user,
                add_signature=pi.add_signature, add_stamp=pi.add_stamp,
            )
            _fill_from_pi(ci, pi)
        messages.success(request, f'Commercial invoice created from {pi.ref_no}. Fill in the shipment details below.')

    if request.method == 'POST':
        if request.POST.get('action') == 'refresh':
            with transaction.atomic():
                _fill_from_pi(ci, pi)
            messages.success(request, 'Items, bank prices and freight were refreshed from the PI.')
            return redirect('bangladeshpi:ci_edit', pk=pi.pk)

        p = request.POST
        ci.invoice_date = _date_or_none(p.get('invoice_date')) or ci.invoice_date
        ci.comm_ref_no = (p.get('comm_ref_no') or '').strip()
        ci.pl_no = (p.get('pl_no') or '').strip()
        ci.issuing_bank_bin = (p.get('issuing_bank_bin') or '').strip()
        ci.our_delivery = (p.get('our_delivery') or '').strip()
        ci.ref_our_ord_no = (p.get('ref_our_ord_no') or '').strip()
        ci.to_order_of = (p.get('to_order_of') or '').strip()
        ci.packages = (p.get('packages') or '').strip()
        ci.lc_number = (p.get('lc_number') or '').strip()
        ci.lc_date = _date_or_none(p.get('lc_date'))
        ci.container_no = (p.get('container_no') or '').strip()
        ci.gross_weight = _dec_or_none(p.get('gross_weight'))
        ci.net_weight = _dec_or_none(p.get('net_weight'))
        ci.volume = _dec_or_none(p.get('volume'))
        ci.hs_code = (p.get('hs_code') or '').strip() or DEFAULT_HS_CODE
        if can_use_seal(request.user):           # only authorised users change the signature/stamp choice
            ci.add_signature = p.get('add_signature') == 'on'
            ci.add_stamp = p.get('add_stamp') == 'on'
        with transaction.atomic():
            ci.save()
            for line in ci.lines.all():
                if line.kind != 'item':
                    continue
                pack = (p.get(f'pack_{line.pk}') or '').strip()
                line.pack = int(pack) if pack.isdigit() else None
                line.gross_kg = _dec_or_none(p.get(f'gross_{line.pk}'))
                line.net_kg = _dec_or_none(p.get(f'net_{line.pk}'))
                line.save(update_fields=['pack', 'gross_kg', 'net_kg'])
        messages.success(request, 'Commercial invoice saved.')
        return redirect('bangladeshpi:ci_edit', pk=pi.pk)

    return render(request, 'bangladeshpi/ci_form.html', {
        'pi': pi, 'ci': ci, 'lines': [l for l in ci.lines.all() if l.kind == 'item'],
        'can_seal': can_use_seal(request.user), 'seal': BangladeshPISeal.get_instance(),
        'pi_changed': (pi.ref_no != ci.pi_ref_no or pi.bank_freight_charges != ci.freight_charges
                       or pi.bank_items_total != ci.cfr_total),
    })


@login_required
def ci_pdf(request, pk):
    pi = get_object_or_404(BangladeshPI, pk=pk)
    ci = get_object_or_404(BangladeshCommercialInvoice.objects.prefetch_related('lines'), pi=pi)
    with_seals = can_use_seal(request.user)
    pdf = build_commercial_invoice_pdf(ci, with_seals=with_seals)
    if with_seals and (ci.add_signature or ci.add_stamp):
        audit_log.info('Signed commercial invoice generated: user=%s invoice=%s signature=%s stamp=%s',
                       request.user.username, ci.invoice_no, ci.add_signature, ci.add_stamp)
    resp = HttpResponse(pdf, content_type='application/pdf')
    disp = 'inline' if request.GET.get('inline') == '1' else 'attachment'
    resp['Content-Disposition'] = f'{disp}; filename="CommercialInvoice_{ci.invoice_no.replace("/", "-")}.pdf"'
    resp['Cache-Control'] = 'no-store'
    return resp


@login_required
def pl_pdf(request, pk):
    """Packing list from the saved commercial invoice (sends you to fill it in first if it does not exist)."""
    pi = get_object_or_404(BangladeshPI, pk=pk)
    ci = BangladeshCommercialInvoice.objects.filter(pi=pi).prefetch_related('lines').first()
    if ci is None:
        messages.info(request, 'Fill in the commercial invoice first; the packing list uses the same details.')
        return redirect('bangladeshpi:ci_edit', pk=pi.pk)
    with_seals = can_use_seal(request.user)
    pdf = build_packing_list_pdf(ci, with_seals=with_seals)
    if with_seals and (ci.add_signature or ci.add_stamp):
        audit_log.info('Signed packing list generated: user=%s invoice=%s signature=%s stamp=%s',
                       request.user.username, ci.invoice_no, ci.add_signature, ci.add_stamp)
    resp = HttpResponse(pdf, content_type='application/pdf')
    disp = 'inline' if request.GET.get('inline') == '1' else 'attachment'
    resp['Content-Disposition'] = f'{disp}; filename="PackingList_{packing_list_no(ci).replace("/", "-")}.pdf"'
    resp['Cache-Control'] = 'no-store'
    return resp
