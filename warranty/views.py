import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.http import JsonResponse, FileResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import (
    WarrantyBrandTerms, WarrantyLetter, WarrantyLetterItem, WarrantyLetterSettings,
    default_item_columns, FIXED_FIELD_KEYS,
)
from .pdf_builder import build_warranty_letter_pdf
from . import services


def _is_manager(user):
    return (user.username or '').strip().lower() == 'manager'


@login_required
def warranty_list(request):
    letters = WarrantyLetter.objects.all()
    company = request.GET.get('company', '')
    if company in dict(WarrantyLetter.COMPANY_CHOICES):
        letters = letters.filter(company=company)
    status = request.GET.get('status', '')
    if status in dict(WarrantyLetter.STATUS_CHOICES):
        letters = letters.filter(status=status)
    return render(request, 'warranty/warranty_list.html', {
        'letters': letters,
        'company_filter': company,
        'status_filter': status,
    })


@login_required
def warranty_form(request, pk=None):
    letter = get_object_or_404(WarrantyLetter, pk=pk) if pk else None

    company = (letter.company if letter else request.GET.get('company', 'junaid'))
    if company not in dict(WarrantyLetter.COMPANY_CHOICES):
        company = 'junaid'

    letter_settings = WarrantyLetterSettings.get_instance(company)
    items_data = [it.data for it in (letter.items.all() if letter else [])]
    item_columns = (letter.item_columns if letter else None) or default_item_columns()
    existing_field_order = (letter.field_order if letter else None) or []

    if not letter:
        # Prefill editable defaults for a brand-new letter. Terms aren't
        # prefilled here since they're looked up per-brand once the user
        # picks a Brand (see the api_brand_terms fetch in the form's JS) --
        # there's no brand chosen yet on a fresh form.
        default_signatory_name = letter_settings.default_signatory_name
        default_signatory_title = letter_settings.default_signatory_title
        default_terms_text = ''
    else:
        default_signatory_name = letter.signatory_name
        default_signatory_title = letter.signatory_title
        default_terms_text = letter.terms_text

    return render(request, 'warranty/warranty_form.html', {
        'letter': letter,
        'company': company,
        'items_data': items_data,
        'item_columns': item_columns,
        'existing_field_order': existing_field_order,
        'default_signatory_name': default_signatory_name,
        'default_signatory_title': default_signatory_title,
        'default_terms_text': default_terms_text,
        'default_intro_template': letter_settings.default_intro_template,
        'ref_no_prefix': letter_settings.ref_no_prefix,
    })


@login_required
@require_POST
def warranty_save(request):
    pk = request.POST.get('letter_id')
    letter = get_object_or_404(WarrantyLetter, pk=pk) if pk else WarrantyLetter()

    # Editing a Pending letter withdraws it back to Draft -- the manager
    # hasn't acted on it yet, so there's nothing to "undo", it just needs
    # resubmitting once the edit is done (handled below via submit_now).
    was_pending = bool(letter.pk) and letter.status == 'Pending'

    company = request.POST.get('company', 'junaid')
    letter.company = company if company in dict(WarrantyLetter.COMPANY_CHOICES) else 'junaid'

    letter.ref_no = (request.POST.get('ref_no') or '').strip()
    letter_date = request.POST.get('letter_date') or ''
    if letter_date:
        letter.letter_date = letter_date

    letter.project = request.POST.get('project', '').strip()
    letter.client = request.POST.get('client', '').strip()
    letter.consultant = request.POST.get('consultant', '').strip()
    letter.main_contractor = request.POST.get('main_contractor', '').strip()

    field_order_json = request.POST.get('field_order_json', '')
    if field_order_json:
        try:
            field_order = json.loads(field_order_json)
        except (ValueError, TypeError):
            field_order = []
        cleaned = []
        for entry in field_order:
            if not isinstance(entry, dict):
                continue
            if entry.get('type') == 'fixed' and entry.get('key') in FIXED_FIELD_KEYS:
                cleaned.append({'type': 'fixed', 'key': entry['key']})
            elif entry.get('type') == 'custom':
                label = (entry.get('label') or '').strip()
                value = (entry.get('value') or '').strip()
                if label and value:
                    cleaned.append({'type': 'custom', 'label': label, 'value': value})
        letter.field_order = cleaned
    else:
        letter.field_order = []

    letter.brand = request.POST.get('brand', '').strip()
    letter.iso_standard = request.POST.get('iso_standard', '').strip()
    letter.intro_text = request.POST.get('intro_text', '').strip()

    letter.terms_text = services.sanitize_rich_text(request.POST.get('terms_text', ''))
    # Remember this letter's terms as the starting default for this brand's
    # next new warranty letter (terms are brand-specific, not company-specific).
    services.save_brand_terms(letter.brand, letter.terms_text)

    letter.signatory_name = request.POST.get('signatory_name', '').strip()
    letter.signatory_title = request.POST.get('signatory_title', '').strip()

    # Item table columns -- freely customized per letter (add/remove/rename
    # via the form UI), posted as an ordered [{key, label}, ...] list.
    columns_json = request.POST.get('columns_json', '')
    columns = []
    if columns_json:
        try:
            parsed = json.loads(columns_json)
        except (ValueError, TypeError):
            parsed = []
        for col in parsed:
            if not isinstance(col, dict):
                continue
            key = (col.get('key') or '').strip()
            label = (col.get('label') or '').strip()
            if key and label:
                columns.append({'key': key, 'label': label})
    letter.item_columns = columns or default_item_columns()

    is_new = letter.pk is None
    if is_new:
        letter.created_by = request.user
    if was_pending:
        letter.status = 'Draft'

    letter.invalidate_pdf()
    letter.save()

    # Items table -- delete and recreate from the posted JSON, mirroring the
    # simple JSON-synced-hidden-field pattern used elsewhere in this codebase
    # (e.g. Submittal.field_order / compliance_rows) but as a real related
    # table since these rows are genuinely tabular data. Row values are
    # free-form key/value pairs keyed by the columns above.
    items_json = request.POST.get('items_json', '')
    letter.items.all().delete()
    if items_json:
        try:
            rows = json.loads(items_json)
        except (ValueError, TypeError):
            rows = []
        valid_keys = {c['key'] for c in letter.item_columns}
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            data = {k: (v or '').strip() for k, v in row.items() if k in valid_keys}
            WarrantyLetterItem.objects.create(letter=letter, display_order=idx, data=data)

    submit_now = request.POST.get('submit_now') == '1'
    if submit_now and letter.status in WarrantyLetter.SUBMITTABLE_STATUSES:
        letter.status = 'Pending'
        letter.submitted_by = request.user
        letter.submitted_at = timezone.now()
        letter.save(update_fields=['status', 'submitted_by', 'submitted_at'])

    return JsonResponse({
        'success': True,
        'letter_id': letter.pk,
        'redirect': f'/warranty/{letter.pk}/',
    })


@login_required
def warranty_detail(request, pk):
    letter = get_object_or_404(WarrantyLetter, pk=pk)
    sig_field = letter.effective_signature_image()
    stamp_field = letter.effective_stamp_image()
    return render(request, 'warranty/warranty_detail.html', {
        'letter': letter,
        'items': letter.items.all(),
        'is_manager': _is_manager(request.user),
        'effective_signature_url': sig_field.url if sig_field else '',
        'effective_stamp_url': stamp_field.url if stamp_field else '',
    })


@login_required
@require_POST
def warranty_delete(request, pk):
    letter = get_object_or_404(WarrantyLetter, pk=pk)
    project_name = (letter.project or '')[:50]
    letter.invalidate_pdf()
    if letter.signature_image:
        letter.signature_image.delete(save=False)
    if letter.stamp_image:
        letter.stamp_image.delete(save=False)
    letter.delete()
    messages.success(request, f'Warranty letter "{project_name}" has been deleted.')
    return redirect('warranty:list')


@login_required
@require_POST
def warranty_submit(request, pk):
    letter = get_object_or_404(WarrantyLetter, pk=pk)
    if _is_manager(request.user):
        messages.error(request, "The manager account can only approve or reject warranty letters.")
        return redirect('warranty:detail', pk=letter.pk)
    if letter.status not in WarrantyLetter.SUBMITTABLE_STATUSES:
        messages.error(request, "Only Draft or Rejected warranty letters can be submitted for approval.")
        return redirect('warranty:detail', pk=letter.pk)

    letter.status = 'Pending'
    letter.submitted_by = request.user
    letter.submitted_at = timezone.now()
    letter.save(update_fields=['status', 'submitted_by', 'submitted_at'])
    messages.success(request, "Warranty letter submitted for approval.")
    return redirect('warranty:detail', pk=letter.pk)


@login_required
@require_POST
def warranty_withdraw(request, pk):
    """Pull a Pending letter back to Draft so it can be edited again, e.g.
    if it was submitted by mistake before the manager has acted on it."""
    letter = get_object_or_404(WarrantyLetter, pk=pk)
    if _is_manager(request.user):
        messages.error(request, "The manager account can only approve or reject warranty letters.")
        return redirect('warranty:detail', pk=letter.pk)
    if letter.status != 'Pending':
        messages.error(request, "Only letters pending approval can be withdrawn.")
        return redirect('warranty:detail', pk=letter.pk)

    letter.status = 'Draft'
    letter.save(update_fields=['status'])
    messages.success(request, "Warranty letter withdrawn back to Draft — you can edit it again.")
    return redirect('warranty:detail', pk=letter.pk)


@login_required
@require_POST
def warranty_update_approval(request, pk):
    letter = get_object_or_404(WarrantyLetter, pk=pk)

    if not _is_manager(request.user):
        messages.error(request, "Only the manager can approve or reject warranty letters.")
        return redirect('warranty:detail', pk=letter.pk)

    if letter.status != 'Pending':
        messages.error(request, "Only letters pending approval can be approved or rejected.")
        return redirect('warranty:detail', pk=letter.pk)

    new_status = (request.POST.get('approval_status') or '').strip()
    if new_status not in ('Approved', 'Rejected'):
        messages.error(request, "Invalid approval status.")
        return redirect('warranty:detail', pk=letter.pk)

    letter.status = new_status
    letter.approval_updated_by = request.user
    letter.approval_updated_at = timezone.now()
    letter.approval_remarks = (request.POST.get('remarks') or '').strip()
    letter.invalidate_pdf()  # DRAFT watermark toggles off once approved
    letter.save(update_fields=[
        'status', 'approval_updated_by', 'approval_updated_at', 'approval_remarks',
        'generated_pdf', 'pdf_generated_at',
    ])
    messages.success(request, f"Warranty letter {new_status.lower()}.")
    return redirect('warranty:detail', pk=letter.pk)


def _save_image_field(request, pk, field_name, post_field_name):
    letter = get_object_or_404(WarrantyLetter, pk=pk)

    if _is_manager(request.user):
        return JsonResponse({'error': 'The manager account can only approve or reject warranty letters.'}, status=403)
    if letter.status != 'Approved':
        return JsonResponse({'error': 'The warranty letter must be Approved first.'}, status=403)

    f = request.FILES.get(post_field_name)
    if not f:
        return JsonResponse({'error': 'Please choose an image.'}, status=400)
    if not (f.content_type or '').startswith('image/'):
        return JsonResponse({'error': 'File must be an image.'}, status=400)

    current = getattr(letter, field_name)
    if current:
        current.delete(save=False)
    setattr(letter, field_name, f)

    letter.invalidate_pdf()
    letter.save()
    return JsonResponse({'success': True, 'url': getattr(letter, field_name).url})


def _clear_image_field(request, pk, field_name):
    letter = get_object_or_404(WarrantyLetter, pk=pk)

    if _is_manager(request.user):
        return JsonResponse({'error': 'The manager account can only approve or reject warranty letters.'}, status=403)
    if letter.status != 'Approved':
        return JsonResponse({'error': 'The warranty letter must be Approved first.'}, status=403)

    current = getattr(letter, field_name)
    if current:
        current.delete(save=False)
    setattr(letter, field_name, None)

    letter.invalidate_pdf()
    letter.save()
    return JsonResponse({'success': True})


@login_required
@require_POST
def warranty_save_signature(request, pk):
    return _save_image_field(request, pk, 'signature_image', 'signature_image')


@login_required
@require_POST
def warranty_clear_signature(request, pk):
    return _clear_image_field(request, pk, 'signature_image')


@login_required
@require_POST
def warranty_save_stamp(request, pk):
    return _save_image_field(request, pk, 'stamp_image', 'stamp_image')


@login_required
@require_POST
def warranty_clear_stamp(request, pk):
    return _clear_image_field(request, pk, 'stamp_image')


@login_required
def warranty_generate_pdf(request, pk):
    letter = get_object_or_404(WarrantyLetter, pk=pk)
    force_regenerate = request.GET.get('regenerate') == '1'
    inline = request.GET.get('inline') == '1'
    ref_part = (letter.ref_no or str(letter.pk)).replace('/', '-')
    filename_dl = f"Warranty_{ref_part}.pdf"

    def _no_cache(response):
        # Browsers/PDF viewers happily cache a GET by URL; since the same
        # preview/download URL can point at different bytes over time (the
        # letter was edited, or the PDF layout logic changed), force a
        # fresh fetch every time instead of silently showing stale content.
        response['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        response['Pragma'] = 'no-cache'
        return response

    if not force_regenerate and letter.generated_pdf and letter.generated_pdf.name:
        try:
            return _no_cache(FileResponse(
                letter.generated_pdf.open('rb'),
                content_type='application/pdf',
                as_attachment=not inline,
                filename=filename_dl,
            ))
        except Exception:
            pass

    pdf_buf = build_warranty_letter_pdf(letter)
    pdf_buf.seek(0)

    if inline:
        return _no_cache(FileResponse(
            pdf_buf,
            content_type='application/pdf',
            as_attachment=False,
            filename=filename_dl,
        ))

    stored_name = f"warranty_{letter.pk}.pdf"
    letter.generated_pdf.save(stored_name, ContentFile(pdf_buf.read()), save=True)
    letter.pdf_generated_at = timezone.now()
    letter.save(update_fields=['pdf_generated_at'])

    return _no_cache(FileResponse(
        letter.generated_pdf.open('rb'),
        content_type='application/pdf',
        as_attachment=True,
        filename=filename_dl,
    ))


@login_required
@require_GET
def api_suggest_ref_no(request):
    company = request.GET.get('company', 'junaid')
    if company not in dict(WarrantyLetter.COMPANY_CHOICES):
        company = 'junaid'
    return JsonResponse({'ref_no': services.suggest_ref_no(company)})


@login_required
@require_GET
def api_brand_terms(request):
    return JsonResponse({'terms_text': services.get_brand_terms(request.GET.get('brand', ''))})


@login_required
def warranty_brand_terms_list(request):
    rows = WarrantyBrandTerms.objects.all().order_by('brand')
    return render(request, 'warranty/brand_terms_list.html', {'rows': rows})


@login_required
def warranty_brand_terms_form(request, pk=None):
    row = get_object_or_404(WarrantyBrandTerms, pk=pk) if pk else None

    if request.method == 'POST':
        brand = request.POST.get('brand', '').strip()
        terms_text = services.sanitize_rich_text(request.POST.get('terms_text', ''))

        if not brand:
            messages.error(request, 'Brand name is required.')
        else:
            clash = WarrantyBrandTerms.objects.filter(brand__iexact=brand)
            if row:
                clash = clash.exclude(pk=row.pk)
            if clash.exists():
                messages.error(request, f'"{brand}" already has saved terms — edit that entry instead of creating a new one.')
            else:
                row = row or WarrantyBrandTerms()
                row.brand = brand
                row.terms_text = terms_text
                row.save()
                messages.success(request, f'Warranty terms for "{brand}" saved.')
                return redirect('warranty:brand_terms_list')

    return render(request, 'warranty/brand_terms_form.html', {'row': row})


@login_required
@require_POST
def warranty_brand_terms_delete(request, pk):
    row = get_object_or_404(WarrantyBrandTerms, pk=pk)
    brand = row.brand
    row.delete()
    messages.success(request, f'Warranty terms for "{brand}" deleted.')
    return redirect('warranty:brand_terms_list')
